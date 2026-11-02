import hashlib
import json
import threading
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .analytics import aggregate
from .cache import suggestion_key
from .models import Community, Evidence, Membership, Search, SearchCommunity, SearchUser, User


class CollectionBusy(Exception):
    pass


def normalize_query(query):
    return " ".join(unicodedata.normalize("NFKC", query).casefold().replace("ё", "е").split())


def utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def upsert(session, model, rows, keys):
    if not rows:
        return
    dialect_insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    # Не превышаем лимит параметров SQLite/PostgreSQL при больших выборках.
    for offset in range(0, len(rows), 200):
        stmt = dialect_insert(model).values(rows[offset:offset + 200])
        updates = {column.name: getattr(stmt.excluded, column.name) for column in model.__table__.columns if column.name not in keys}
        session.execute(stmt.on_conflict_do_update(index_elements=keys, set_=updates))


class SearchService:
    def __init__(self, settings, session_factory, collector, cache, now=None, credential_partition=None):
        self.settings = settings
        self.sessions = session_factory
        self.collector = collector
        self.cache = cache
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.local_lock = threading.Lock()
        self.credential_partition = credential_partition

    def query_key(self, query, modes):
        payload = [normalize_query(query), sorted(set(modes)), self.settings.data_mode, self.settings.policy_key()]
        if self.credential_partition is not None:
            # Разные VK-аккаунты могут иметь разные права на стены/сообщества.
            payload.append(self.credential_partition())
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()

    def _find(self, session, key):
        cached_id = self.cache.get("query:" + key)
        row = session.get(Search, cached_id) if isinstance(cached_id, int) else None
        if row is None or row.key != key:
            row = session.scalar(select(Search).where(Search.key == key))
        return row

    def _fresh(self, row):
        return row is not None and row.expires_at is not None and utc(row.expires_at) > self.now()

    def search(self, query, modes):
        query = " ".join(query.split())
        modes = sorted(set(modes))
        key = self.query_key(query, modes)
        with self.sessions() as session:
            row = self._find(session, key)
            if self._fresh(row):
                return self.dashboard(session, row, True)

        if not self.local_lock.acquire(blocking=False):
            raise CollectionBusy("Уже выполняется сбор аудитории. Повторите поиск после его окончания.")
        try:
            with self.sessions.begin() as session:
                # Один сбор на БД: защищает от дублей и общего лимита VK даже в двух процессах.
                if session.bind.dialect.name == "postgresql":
                    locked = session.scalar(text("SELECT pg_try_advisory_xact_lock(724191854)"))
                    if not locked:
                        raise CollectionBusy("Уже выполняется сбор аудитории. Повторите поиск позже.")
                row = session.scalar(select(Search).where(Search.key == key))
                if self._fresh(row):
                    return self.dashboard(session, row, True)

                result = self.collector.collect(normalize_query(query), modes)
                completed = self.now()
                if not result.complete and row is not None and row.details.get("complete", True):
                    dashboard = self.dashboard(session, row, True)
                    dashboard["complete"] = False
                    dashboard["warnings"] = [
                        "Обновление не завершено: показана предыдущая выборка от "
                        + utc(row.collected_at).isoformat() + ". Повторите поиск позже.",
                        *result.warnings,
                    ]
                    return dashboard
                if row is None:
                    row = Search(key=key, query=query, normalized_query=normalize_query(query), modes=modes, mode=self.settings.data_mode)
                    session.add(row)
                    session.flush()
                self.save_collection(session, row, result, completed)
                row.query = query
                row.collected_at = completed
                # Неполный ответ можно показать, но повторный поиск обязан попытаться дополнить его.
                row.expires_at = completed + timedelta(seconds=self.settings.cache_ttl_seconds if result.complete else 0)
                row.details = {"warnings": result.warnings, "limited": result.limited, "complete": result.complete}
                if self.credential_partition is not None:
                    row.details["credential_partition"] = self.credential_partition()
                session.flush()
                dashboard = self.dashboard(session, row, False)
                search_id = row.id
            self.cache.set("query:" + key, search_id, self.settings.cache_ttl_seconds)
            return dashboard
        finally:
            self.local_lock.release()

    def save_collection(self, session, row, result, completed):
        mode = self.settings.data_mode
        user_rows = []
        for user_id, profile in result.users.items():
            city = profile.get("city") if isinstance(profile.get("city"), dict) else {}
            country = profile.get("country") if isinstance(profile.get("country"), dict) else {}
            user_rows.append({
                "mode": mode, "id": user_id,
                "first_name": profile.get("first_name"), "last_name": profile.get("last_name"),
                "sex": profile.get("sex"), "bdate": profile.get("bdate"),
                "city_id": city.get("id"), "city": profile.get("city_title") or city.get("title"),
                "country": profile.get("country_title") or country.get("title"),
                "country_code": profile.get("country_code"), "region": profile.get("region"),
                "updated_at": completed,
            })
        upsert(session, User, user_rows, ["mode", "id"])
        group_rows = [{"mode": mode, "id": group_id, "name": group.get("name", str(group_id)),
                       "screen_name": group.get("screen_name"), "members_count": group.get("members_count"),
                       "updated_at": completed} for group_id, group in result.groups.items()]
        upsert(session, Community, group_rows, ["mode", "id"])
        for model in (Membership, Evidence, SearchUser, SearchCommunity):
            session.execute(delete(model).where(model.search_id == row.id))
        if result.users:
            session.execute(insert(SearchUser), [{"search_id": row.id, "user_id": uid, "mode": mode} for uid in result.users])
        if result.groups:
            session.execute(insert(SearchCommunity), [{"search_id": row.id, "group_id": gid, "mode": mode} for gid in result.groups])
        memberships = [{"search_id": row.id, "group_id": gid, "user_id": uid}
                       for gid, uid in sorted(result.memberships) if uid in result.users and gid in result.groups]
        if memberships:
            session.execute(insert(Membership), memberships)
        evidence = [{"search_id": row.id, "user_id": uid, "source": source, "source_id": str(source_id)}
                    for uid, source, source_id in sorted(result.evidence) if uid in result.users]
        if evidence:
            session.execute(insert(Evidence), evidence)

    def dashboard(self, session, row, cache_hit):
        users = list(session.scalars(select(User).join(SearchUser, (SearchUser.user_id == User.id) & (SearchUser.mode == User.mode)).where(SearchUser.search_id == row.id)))
        groups = list(session.scalars(select(Community).join(SearchCommunity, (SearchCommunity.group_id == Community.id) & (SearchCommunity.mode == Community.mode)).where(SearchCommunity.search_id == row.id)))
        memberships = Counter(session.scalars(select(Membership.group_id).where(Membership.search_id == row.id)))
        source_users = {source: set() for source in ("groups", "likes", "reposts")}
        for source, user_id in session.execute(select(Evidence.source, Evidence.user_id).where(Evidence.search_id == row.id)):
            source_users.setdefault(source, set()).add(user_id)
        return {
            "query": row.query, "normalized_query": row.normalized_query, "modes": row.modes,
            "mode": row.mode, "collected_at": utc(row.collected_at).isoformat(),
            "expires_at": utc(row.expires_at).isoformat(), "cache_hit": cache_hit,
            "limited": row.details.get("limited", False), "complete": row.details.get("complete", True),
            "warnings": row.details.get("warnings", []),
            "groups": [{"id": group.id, "name": group.name, "screen_name": group.screen_name,
                        "members_count": group.members_count, "collected_count": memberships[group.id]} for group in groups],
            "sources": {source: len(ids) for source, ids in source_users.items()},
            **aggregate(users, today=self.now().date()),
        }

    def suggestions(self, query):
        prefix = normalize_query(query)
        cache_key = suggestion_key(self.settings.data_mode, prefix)
        partition = self.credential_partition() if self.credential_partition is not None else None
        if partition is not None:
            cache_key += ":" + hashlib.sha256(partition.encode()).hexdigest()
        # Источник подсказок — БД. TTL короткий: новая тема появится не позже чем через 30 сек.
        cached = self.cache.get(cache_key)
        if isinstance(cached, list) and cached:
            return cached
        with self.sessions() as session:
            statement = select(Search.normalized_query, Search.query).where(
                Search.mode == self.settings.data_mode,
                Search.normalized_query.startswith(prefix, autoescape=True),
                Search.collected_at.is_not(None),
            )
            if partition is not None:
                statement = statement.where(Search.details["credential_partition"].as_string() == partition)
            rows = session.execute(statement.order_by(Search.collected_at.desc()).limit(100))
            seen, suggestions = set(), []
            for normalized, original in rows:
                if normalized not in seen:
                    suggestions.append(original)
                    seen.add(normalized)
                if len(suggestions) == 8:
                    break
        self.cache.set(cache_key, suggestions, 30)
        return suggestions
