from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.collectors import Collection, CollectionError
from app.models import Community, Evidence, Membership, Search, SearchUser, User
from app.service import CollectionBusy, SearchService, normalize_query


def test_normalization_and_24h_cache_survive_redis_flush(service):
    first = service.search("  Ёлки   и КНИГИ ", ["groups", "likes"])
    assert first["total_users"] == 2
    assert first["sources"] == {"groups": 2, "likes": 1, "reposts": 0}
    service.cache.client.flushall()
    service.now.value += timedelta(hours=23, minutes=59)
    second = service.search("елки и книги", ["likes", "groups"])
    assert second["cache_hit"] is True
    assert service.collector.calls == 1
    service.now.value += timedelta(minutes=1)
    assert service.search("елки и книги", ["groups", "likes"])["cache_hit"] is False
    assert service.collector.calls == 2


def test_refresh_upserts_profiles_and_replaces_query_membership(service):
    service.search("Книги", ["groups"])
    service.now.value += timedelta(days=1, seconds=1)
    service.collector.result = Collection(
        users={1: {"sex": 0}, 3: {"sex": 2, "bdate": "3.1.2002"}},
        groups={10: {"name": "Новое название"}},
        memberships={(10, 1), (10, 3)},
        evidence={(1, "groups", "10"), (3, "groups", "10")},
    )
    result = service.search("книги", ["groups"])
    assert result["total_users"] == 2
    assert result["groups"][0]["name"] == "Новое название"
    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 3
        profile = session.get(User, ("demo", 1))
        assert profile.sex == 0 and profile.bdate is None and profile.city is None
        assert set(session.scalars(select(SearchUser.user_id))) == {1, 3}
        assert set(session.scalars(select(Membership.user_id))) == {1, 3}
        assert set(session.scalars(select(Evidence.user_id))) == {1, 3}
        assert session.scalar(select(func.count()).select_from(Community)) == 1


def test_different_scenarios_and_mode_and_limits_have_distinct_keys(service):
    assert normalize_query("  КОФЕ\t с  молоком ") == "кофе с молоком"
    a = service.query_key("Книги", ["groups"])
    assert a != service.query_key("Книги", ["likes"])
    service.settings.max_groups += 1
    assert a != service.query_key("Книги", ["groups"])
    service.settings.max_groups -= 1
    service.settings.data_mode = "vk"
    assert a != service.query_key("Книги", ["groups"])


def test_error_or_partial_refresh_preserves_last_complete_collection(service):
    original = service.search("Книги", ["groups"])
    service.now.value += timedelta(days=2)
    service.collector.error = CollectionError("VK недоступен")
    with pytest.raises(CollectionError):
        service.search("Книги", ["groups"])
    service.collector.error = None
    service.collector.result = Collection(users={3: {"sex": 1}}, complete=False, warnings=["Нет доступа"])
    result = service.search("Книги", ["groups"])
    assert result["total_users"] == original["total_users"]
    assert result["collected_at"] == original["collected_at"]
    assert result["complete"] is False and "предыдущая" in result["warnings"][0]
    with service.sessions() as session:
        assert session.get(User, ("demo", 3)) is None
        assert set(session.scalars(select(SearchUser.user_id))) == {1, 2}
    service.search("Книги", ["groups"])
    assert service.collector.calls == 4  # Failed refresh was not cached.


def test_partial_first_collection_retries_and_successful_empty_is_cached(service):
    service.collector.result.complete = False
    first = service.search("Книги", ["groups"])
    assert first["expires_at"] == first["collected_at"]
    service.search("Книги", ["groups"])
    assert service.collector.calls == 2
    service.collector.result = Collection()
    assert service.search("Книги", ["groups"])["total_users"] == 0
    assert service.search("Книги", ["groups"])["cache_hit"]
    assert service.collector.calls == 3


def test_one_active_collection_and_cached_requests_allowed(service):
    service.search("Книги", ["groups"])
    service.local_lock.acquire()
    try:
        assert service.search("Книги", ["groups"])["cache_hit"]
        with pytest.raises(CollectionBusy):
            service.search("Спорт", ["groups"])
    finally:
        service.local_lock.release()


def test_suggestions_prefix_escaped_and_mode_isolated(service):
    service.search("100% Книги", ["groups"])
    service.search("1000 велосипедов", ["groups"])
    service.search("Книги", ["groups"])
    service.search("КНИГИ", ["likes"])
    assert service.suggestions("100%") == ["100% Книги"]
    assert len(service.suggestions("кн")) == 1
    service.settings.data_mode = "vk"
    assert service.suggestions("кн") == []


def test_demo_and_real_profiles_do_not_overwrite_each_other(service):
    service.search("Книги", ["groups"])
    other = SearchService(service.settings.model_copy(update={"data_mode": "vk"}), service.sessions, service.collector, service.cache, service.now)
    other.collector.result = Collection(users={1: {"sex": 2}})
    other.search("Книги", ["groups"])
    with service.sessions() as session:
        assert session.get(User, ("demo", 1)).sex == 1
        assert session.get(User, ("vk", 1)).sex == 2
        assert session.scalar(select(func.count()).select_from(Search)) == 2


def test_storage_failure_rolls_back_profiles_and_query_links(service, monkeypatch):
    original = service.search("Книги", ["groups"])
    service.now.value += timedelta(days=2)
    service.collector.result = Collection(users={1: {"sex": 2}, 3: {"sex": 1}})
    save = service.save_collection

    def fail_after_write(*args):
        save(*args)
        raise RuntimeError("simulated failure before commit")

    monkeypatch.setattr(service, "save_collection", fail_after_write)
    with pytest.raises(RuntimeError):
        service.search("Книги", ["groups"])
    with service.sessions() as session:
        assert session.get(User, ("demo", 1)).sex == 1
        assert session.get(User, ("demo", 3)) is None
        assert set(session.scalars(select(SearchUser.user_id))) == {1, 2}
        assert service.dashboard(session, session.scalar(select(Search)), True)["collected_at"] == original["collected_at"]


def test_redis_outage_does_not_trigger_vk_refetch_or_break_suggestions(service, monkeypatch):
    from redis.exceptions import ConnectionError

    service.search("Книги", ["groups"])

    def disconnected(*args, **kwargs):
        raise ConnectionError("offline")

    monkeypatch.setattr(service.cache.client, "get", disconnected)
    monkeypatch.setattr(service.cache.client, "setex", disconnected)
    assert service.search("книги", ["groups"])["cache_hit"]
    assert service.collector.calls == 1
    assert service.suggestions("кн") == ["Книги"]
