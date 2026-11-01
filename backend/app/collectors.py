"""Bounded collectors of publicly available profiles and reproducible demo data."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from .vk import VkApiError, VkClient


@dataclass
class Collection:
    users: dict[int, dict] = field(default_factory=dict)
    groups: dict[int, dict] = field(default_factory=dict)
    memberships: set[tuple[int, int]] = field(default_factory=set)
    evidence: set[tuple[int, str, str]] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    limited: bool = False
    source_stats: dict[str, int] = field(default_factory=dict)
    complete: bool = True


class CollectionError(RuntimeError):
    """Safe collector failure; the caller must retain the last successful result."""


COUNTRY_CODES = {
    "россия": "RUS", "russia": "RUS", "russian federation": "RUS",
    "украина": "UKR", "ukraine": "UKR",
    "беларусь": "BLR", "белоруссия": "BLR", "belarus": "BLR",
    "казахстан": "KAZ", "kazakhstan": "KAZ",
    "азербайджан": "AZE", "azerbaijan": "AZE",
    "армения": "ARM", "armenia": "ARM",
    "грузия": "GEO", "georgia": "GEO",
    "кыргызстан": "KGZ", "киргизия": "KGZ", "kyrgyzstan": "KGZ",
    "молдова": "MDA", "молдавия": "MDA", "moldova": "MDA",
    "таджикистан": "TJK", "tajikistan": "TJK",
    "туркменистан": "TKM", "туркмения": "TKM", "turkmenistan": "TKM",
    "узбекистан": "UZB", "uzbekistan": "UZB",
}


class VkCollector:
    def __init__(self, settings: Any, client: VkClient | None = None):
        self.settings = settings
        self.client = client or VkClient(settings)

    def close(self) -> None:
        self.client.close()

    def collect(self, query: str, modes: list[str]) -> Collection:
        result = Collection()
        deadline = time.monotonic() + self.settings.collection_timeout_seconds
        ids: set[int] = set()
        successful_sources: set[str] = set()
        if "groups" in modes:
            try:
                if self._groups(query, result, ids, deadline):
                    successful_sources.add("groups")
            except VkApiError as error:
                self._warn(result, str(error))
        reaction_modes = [mode for mode in modes if mode in ("likes", "reposts")]
        if reaction_modes:
            try:
                posts = self._posts(query, result, deadline)
                for mode in reaction_modes:
                    if self._reactions(posts, mode, result, ids, deadline):
                        successful_sources.add(mode)
            except VkApiError as error:
                self._warn(result, str(error))
        if not successful_sources:
            raise CollectionError("Не удалось собрать выбранные источники. " + " ".join(result.warnings))
        if ids:
            self._profiles(ids, result, deadline)
            if not result.users and not result.complete:
                raise CollectionError("VK не вернул доступных профилей. " + " ".join(result.warnings))
            self._geography(result, deadline)
        result.memberships = {(group_id, user_id) for group_id, user_id in result.memberships if user_id in result.users}
        result.evidence = {item for item in result.evidence if item[0] in result.users}
        for mode in modes:
            result.source_stats[mode] = len({uid for uid, source, _ in result.evidence if source == mode})
        if result.limited:
            result.warnings.append("Достигнуты лимиты сбора: показана ограниченная выборка, а не вся аудитория VK.")
        result.warnings = list(dict.fromkeys(result.warnings))
        return result

    @staticmethod
    def _warn(result: Collection, message: str) -> None:
        result.complete = False
        if message not in result.warnings:
            result.warnings.append(message)

    def _add(self, raw_id: Any, source: str, source_id: str, result: Collection, ids: set[int]) -> bool:
        if isinstance(raw_id, dict):
            raw_id = raw_id.get("id")
        if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
            return False  # Community actions are not individual audience members.
        if raw_id not in ids and len(ids) >= self.settings.max_users:
            result.limited = True
            return False
        ids.add(raw_id)
        result.evidence.add((raw_id, source, source_id))
        return True

    def _groups(self, query: str, result: Collection, ids: set[int], deadline: float) -> bool:
        response = self.client.call("groups.search", deadline=deadline, q=query, count=self.settings.max_groups, offset=0)
        groups = response.get("items", [])
        if response.get("count", len(groups)) > len(groups):
            result.limited = True
        succeeded = not groups
        eligible = 0
        for group in groups[: self.settings.max_groups]:
            group_id = group.get("id")
            if not isinstance(group_id, int) or group_id <= 0 or group.get("is_closed", 0) != 0 or group.get("deactivated"):
                continue
            eligible += 1
            result.groups[group_id] = {
                "id": group_id,
                "name": group.get("name", "Сообщество"),
                "screen_name": group.get("screen_name", ""),
                "members_count": group.get("members_count"),
            }
            offset = 0
            while offset < self.settings.max_members_per_group:
                try:
                    page = self.client.call(
                        "groups.getMembers", deadline=deadline, group_id=group_id,
                        sort="id_asc", offset=offset,
                        count=min(1000, self.settings.max_members_per_group - offset),
                    )
                except VkApiError as error:
                    self._warn(result, str(error))
                    break
                succeeded = True
                members = page.get("items", [])
                result.groups[group_id]["members_count"] = page.get("count")
                for member in members:
                    if self._add(member, "groups", str(group_id), result, ids):
                        user_id = member.get("id") if isinstance(member, dict) else member
                        result.memberships.add((group_id, user_id))
                offset += len(members)
                total = page.get("count", offset)
                if not members or offset >= total:
                    break
                if offset >= self.settings.max_members_per_group or len(ids) >= self.settings.max_users:
                    result.limited = True
                    break
        if groups and eligible == 0:
            result.warnings.append("В найденной выборке нет открытых сообществ; закрытые сообщества пропущены.")
            succeeded = True
        return succeeded

    def _posts(self, query: str, result: Collection, deadline: float) -> list[dict]:
        posts: list[dict] = []
        seen: set[tuple[int, int]] = set()
        cursor = ""
        cursors: set[str] = set()
        end_time = int(time.time())
        while len(posts) < self.settings.max_posts:
            params = {
                "q": query, "count": min(200, self.settings.max_posts - len(posts)),
                "start_time": end_time - self.settings.post_search_days * 86400,
                "end_time": end_time,
            }
            if cursor:
                params["start_from"] = cursor
            page = self.client.call("newsfeed.search", deadline=deadline, **params)
            items = page.get("items", [])
            new_count = 0
            for post in items:
                owner = post.get("owner_id", post.get("from_id"))
                post_id = post.get("id")
                if not isinstance(owner, int) or not owner or not isinstance(post_id, int) or post_id <= 0:
                    continue
                key = (owner, post_id)
                if key not in seen:
                    seen.add(key)
                    posts.append({"owner_id": owner, "id": post_id})
                    new_count += 1
                    if len(posts) == self.settings.max_posts:
                        break
            next_cursor = page.get("next_from")
            total = page.get("total_count", page.get("count", len(posts)))
            if len(posts) >= self.settings.max_posts:
                result.limited |= bool(next_cursor) or total > len(posts)
                break
            if not items or not next_cursor:
                break
            if next_cursor in cursors or new_count == 0:
                self._warn(result, "newsfeed.search: VK повторил страницу; дальнейший обход остановлен.")
                break
            cursors.add(next_cursor)
            cursor = next_cursor
        return posts

    def _reactions(self, posts: list[dict], mode: str, result: Collection, ids: set[int], deadline: float) -> bool:
        succeeded = not posts
        for post in posts:
            offset = 0
            while offset < self.settings.max_reactions_per_post:
                try:
                    page = self.client.call(
                        "likes.getList", deadline=deadline, type="post",
                        owner_id=post["owner_id"], item_id=post["id"],
                        filter="copies" if mode == "reposts" else "likes",
                        extended=0, offset=offset,
                        count=min(1000, self.settings.max_reactions_per_post - offset),
                    )
                except VkApiError as error:
                    self._warn(result, str(error))
                    break
                succeeded = True
                items = page.get("items", [])
                for user_id in items:
                    self._add(user_id, mode, f"{post['owner_id']}_{post['id']}", result, ids)
                offset += len(items)
                if not items or offset >= page.get("count", offset):
                    break
                if offset >= self.settings.max_reactions_per_post or len(ids) >= self.settings.max_users:
                    result.limited = True
                    break
        return succeeded

    def _profiles(self, ids: set[int], result: Collection, deadline: float) -> None:
        ordered = sorted(ids)
        fields = "sex,bdate,city,country"
        for start in range(0, len(ordered), 1000):
            batch = ordered[start : start + 1000]
            try:
                try:
                    profiles = self.client.call("users.get", deadline=deadline, user_ids=",".join(map(str, batch)), fields=fields)
                except VkApiError as error:
                    if error.code != 100 or "country" not in fields:
                        raise
                    # Current schema omits country although the method prose lists it.
                    # Recover without it; missing countries remain unknown everywhere.
                    fields = "sex,bdate,city"
                    result.warnings.append("VK отклонил поле country: страны и часть регионов могут быть не определены.")
                    profiles = self.client.call("users.get", deadline=deadline, user_ids=",".join(map(str, batch)), fields=fields)
            except VkApiError as error:
                self._warn(result, str(error))
                continue
            for profile in profiles:
                uid = profile.get("id")
                if uid not in ids or profile.get("deactivated") or profile.get("is_closed"):
                    continue
                # Never store arbitrary raw VK payloads (contacts, religion, politics, etc.).
                result.users[uid] = {key: profile.get(key) for key in ("id", "first_name", "last_name", "sex", "bdate", "city", "country")}

    def _geography(self, result: Collection, deadline: float) -> None:
        city_regions: dict[tuple[int, int], str | None] = {}
        lookups = 0
        max_lookups = getattr(self.settings, "max_city_lookups", 20)
        for profile in result.users.values():
            city = profile.get("city") or {}
            country = profile.get("country") or {}
            if not isinstance(city, dict):
                city = {}
            if not isinstance(country, dict):
                country = {}
            profile["city_title"] = city.get("title")
            profile["country_title"] = country.get("title")
            profile["country_code"] = COUNTRY_CODES.get(str(country.get("title", "")).casefold())
            profile["region"] = None
            city_id, country_id = city.get("id"), country.get("id")
            if not city_id or not country_id or not city.get("title"):
                continue
            key = (country_id, city_id)
            if key not in city_regions:
                if lookups >= max_lookups:
                    if "Лимит уточнения регионов достигнут; часть городов показана без региона." not in result.warnings:
                        result.warnings.append("Лимит уточнения регионов достигнут; часть городов показана без региона.")
                    continue
                lookups += 1
                try:
                    response = self.client.call(
                        "database.getCities", deadline=deadline, country_id=country_id,
                        q=city["title"], count=1000, need_all=1,
                    )
                    # Exact VK ID match only: equal city names are not evidence.
                    match = next((entry for entry in response.get("items", []) if entry.get("id") == city_id), None)
                    city_regions[key] = match.get("region") if match else None
                except VkApiError as error:
                    self._warn(result, str(error))
                    city_regions[key] = None
            profile["region"] = city_regions.get(key)


# Deliberately fictional IDs, names and memberships, independent of real VK users.
DEMO_LOCATIONS = [
    ("Россия", "RUS", "Москва", "Москва"),
    ("Россия", "RUS", "Санкт-Петербург", "Санкт-Петербург"),
    ("Россия", "RUS", "Московская область", "Подольск"),
    ("Россия", "RUS", "Республика Татарстан", "Казань"),
    ("Россия", "RUS", "Новосибирская область", "Новосибирск"),
    ("Беларусь", "BLR", "Минск", "Минск"),
    ("Беларусь", "BLR", "Брестская область", "Брест"),
    ("Казахстан", "KAZ", "Алматы", "Алматы"),
    ("Казахстан", "KAZ", "Астана", "Астана"),
    ("Узбекистан", "UZB", "Ташкент", "Ташкент"),
    ("Армения", "ARM", "Ереван", "Ереван"),
    ("Кыргызстан", "KGZ", "Бишкек", "Бишкек"),
    ("Молдова", "MDA", "Кишинёв", "Кишинёв"),
    (None, None, None, None),
]


class DemoCollector:
    def close(self) -> None:
        pass

    def collect(self, query: str, modes: list[str]) -> Collection:
        result = Collection(warnings=["Демонстрационный режим: все профили, связи и статистика вымышлены."])
        seed = int.from_bytes(hashlib.sha256(query.encode("utf-8")).digest()[:4], "big")
        start = seed % 240
        sets = {
            "groups": {(start + index) % 240 for index in range(120)},
            "likes": {(start + index) % 240 for index in range(70, 155)},
            "reposts": {(start + index) % 240 for index in range(110, 165)},
        }
        for mode in modes:
            for index in sorted(sets[mode]):
                uid = 9_000_000_000_000 + index
                country, code, region, city = DEMO_LOCATIONS[index % len(DEMO_LOCATIONS)]
                result.users[uid] = {
                    "id": uid, "first_name": "Демо", "last_name": f"Участник {index + 1}",
                    "sex": 0 if index % 13 == 0 else (index % 2) + 1,
                    "bdate": None if index % 7 == 0 else ("12.6" if index % 11 == 0 else f"{index % 28 + 1}.{index % 12 + 1}.{1962 + index % 45}"),
                    "city": {"id": index % 14 + 1, "title": city} if city else None,
                    "country": {"title": country} if country else None,
                    "city_title": city, "country_title": country, "country_code": code, "region": region,
                }
                if mode == "groups":
                    gid = 8_000_000_000_000 + seed * 3 + index % 3
                    result.groups[gid] = {"id": gid, "name": f"Демо: {query} · сообщество {index % 3 + 1}", "screen_name": "", "members_count": 40}
                    result.memberships.add((gid, uid))
                    source_id = str(gid)
                else:
                    source_id = f"demo_{seed}_{mode}_{index % 4}"
                result.evidence.add((uid, mode, source_id))
            result.source_stats[mode] = len(sets[mode])
        return result
