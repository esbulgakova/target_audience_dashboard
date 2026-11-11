from types import SimpleNamespace
from urllib.parse import parse_qs
import time

import httpx
import pytest

from app.collectors import CollectionError, DemoCollector, VkCollector
from app.vk import VkApiError, VkClient


def settings(**overrides):
    return SimpleNamespace(**{
        "vk_token": "test-secret-never-log", "vk_api_version": "5.199",
        "vk_api_base_url": "https://api.vk.com/method", "vk_request_interval": 0,
        "vk_timeout_seconds": 2, "collection_timeout_seconds": 60,
        "max_groups": 3, "max_members_per_group": 1500, "max_posts": 10,
        "max_reactions_per_post": 1000, "max_users": 3000,
        "post_search_days": 30, "max_city_lookups": 20,
        **overrides,
    })


class StubVk:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def call(self, method, *, deadline, **params):
        self.calls.append((method, params))
        return self.handler(method, params)

    def close(self):
        pass


def test_vk_errors_do_not_include_token_or_remote_error_text():
    def respond(request):
        assert "test-secret" not in str(request.url)
        assert request.headers["Authorization"] == "Bearer test-secret-never-log"
        assert "access_token" not in parse_qs(request.content.decode())
        assert parse_qs(request.content.decode())["lang"] == ["ru"]
        return httpx.Response(200, json={"error": {
            "error_code": 5, "error_msg": "leak test-secret-never-log",
            "request_params": [{"key": "access_token", "value": "test-secret-never-log"}],
        }})

    client = VkClient(settings(), httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(VkApiError) as caught:
        client.call("groups.search", deadline=time.monotonic() + 5, q="music")
    assert caught.value.code == 5
    assert "токен VK недействителен" in str(caught.value)
    assert "test-secret" not in str(caught.value)


def test_profile_type_denial_is_safe_and_does_not_retry_or_refresh():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"error": {
            "error_code": 1051,
            "error_msg": "Method is not available for this profile type: test-secret-never-log",
            "request_params": [{"key": "access_token", "value": "test-secret-never-log"}],
        }})

    provider = SimpleNamespace(
        access_token=lambda **kwargs: "test-secret-never-log",
        refresh_after_rejection=lambda *args, **kwargs: pytest.fail("Access denial must not refresh the token"),
    )
    client = VkClient(settings(), httpx.Client(transport=httpx.MockTransport(respond)), token_provider=provider)
    with pytest.raises(VkApiError) as caught:
        client.call("groups.search", deadline=time.monotonic() + 5, q="music")
    message = str(caught.value)
    assert caught.value.code == 1051 and caught.value.method == "groups.search"
    assert "метод недоступен для типа доступа, выданного этому приложению" in message
    assert "Разрешения на этот метод нужно уточнить у VK" in message
    assert "1051" in message and len(calls) == 1
    assert "test-secret" not in message and "request_params" not in message
    assert "Method is not available" not in message


def test_rate_limit_retries_are_bounded(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"error": {"error_code": 6}})

    monkeypatch.setattr(VkClient, "_backoff", lambda *args: None)
    client = VkClient(settings(), httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(VkApiError) as caught:
        client.call("groups.search", deadline=time.monotonic() + 5, q="music")
    assert len(calls) == 3
    assert caught.value.code == 6


def test_network_errors_are_sanitized():
    def respond(request):
        raise httpx.ConnectError("secret from transport", request=request)

    client = VkClient(settings(), httpx.Client(transport=httpx.MockTransport(respond)))
    client._backoff = lambda *args: None
    with pytest.raises(VkApiError) as caught:
        client.call("groups.search", deadline=time.monotonic() + 5)
    assert "secret from transport" not in str(caught.value)
    assert "связаться с VK" in str(caught.value)


@pytest.mark.parametrize(("method", "payload"), [
    ("groups.search", []),
    ("groups.getMembers", {"items": None}),
    ("newsfeed.search", {"items": [42]}),
    ("likes.getList", {"items": [1], "count": "two"}),
    ("users.get", {}),
    ("users.get", [None]),
])
def test_invalid_responses_raise_safe_errors(method, payload):
    client = VkClient(settings(), httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": payload})
    )))
    with pytest.raises(VkApiError, match="неожиданного формата"):
        client.call(method, deadline=time.monotonic() + 5)


def test_expired_deadline_never_calls_network():
    client = VkClient(settings(), httpx.Client(transport=httpx.MockTransport(
        lambda request: pytest.fail("Expired requests must not reach VK")
    )))
    with pytest.raises(VkApiError, match="превышено время"):
        client.call("groups.search", deadline=time.monotonic() - 1)


def test_combined_collection_paginates_deduplicates_and_excludes_closed_users():
    def respond(method, params):
        if method == "groups.search":
            return {"count": 1, "items": [{"id": 10, "name": "Music", "is_closed": 0}]}
        if method == "groups.getMembers":
            if params["offset"] == 0:
                assert params["count"] == 1000
                return {"count": 1002, "items": list(range(1, 1001))}
            assert params["offset"] == 1000
            return {"count": 1002, "items": [1001, 1002]}
        if method == "newsfeed.search":
            return {"items": [{"owner_id": -10, "id": 42}], "count": 1}
        if method == "likes.getList":
            return {"count": 4, "items": [1, 1001, 1003, -20]}
        if method == "users.get":
            return [{"id": int(uid), "sex": 1, "is_closed": int(uid) == 2,
                     "deactivated": "deleted" if int(uid) == 3 else None}
                    for uid in params["user_ids"].split(",")]
        raise AssertionError(method)

    stub = StubVk(respond)
    result = VkCollector(settings(), stub).collect("музыка", ["groups", "likes", "reposts"])
    assert len(result.users) == 1001
    assert 2 not in result.users and 3 not in result.users and -20 not in result.users
    assert len(result.memberships) == 1000
    assert result.source_stats == {"groups": 1000, "likes": 3, "reposts": 3}
    assert len([method for method, _ in stub.calls if method == "users.get"]) == 2
    assert result.complete


def test_partial_failure_is_not_a_fresh_complete_result():
    def respond(method, params):
        if method == "groups.search":
            return {"count": 1, "items": [{"id": 10, "name": "Music"}]}
        if method == "groups.getMembers":
            return {"count": 1, "items": [1]}
        if method == "newsfeed.search":
            raise VkApiError(method, "приложению не разрешён этот метод.", 7)
        if method == "users.get":
            return [{"id": 1, "sex": 2}]
        raise AssertionError(method)

    result = VkCollector(settings(), StubVk(respond)).collect("музыка", ["groups", "likes"])
    assert set(result.users) == {1}
    assert not result.complete
    assert result.source_stats == {"groups": 1, "likes": 0}
    assert "newsfeed.search" in result.warnings[0]


def test_all_sources_denied_raise_instead_of_caching_empty_audience():
    def respond(method, params):
        raise VkApiError(method, "нет прав", 7)

    with pytest.raises(CollectionError, match="Не удалось собрать"):
        VkCollector(settings(), StubVk(respond)).collect("музыка", ["groups", "reposts"])


def test_global_limit_does_not_count_extra_users():
    def respond(method, params):
        if method == "groups.search":
            return {"count": 1, "items": [{"id": 10}]}
        if method == "groups.getMembers":
            return {"count": 4, "items": [1, 2, 3, 4]}
        if method == "users.get":
            return [{"id": int(uid)} for uid in params["user_ids"].split(",")]
        raise AssertionError(method)

    result = VkCollector(settings(max_users=2), StubVk(respond)).collect("музыка", ["groups"])
    assert set(result.users) == {1, 2}
    assert result.limited
    assert result.complete


def test_country_field_fallback_does_not_guess_geography():
    def respond(method, params):
        if method == "groups.search":
            return {"count": 1, "items": [{"id": 10}]}
        if method == "groups.getMembers":
            return {"count": 1, "items": [1]}
        if method == "users.get":
            if "country" in params["fields"]:
                raise VkApiError(method, "invalid fields", 100)
            return [{"id": 1, "city": {"id": 1, "title": "Москва"}}]
        raise AssertionError(method)

    result = VkCollector(settings(), StubVk(respond)).collect("музыка", ["groups"])
    assert result.users[1]["city_title"] == "Москва"
    assert result.users[1]["country_code"] is None
    assert result.users[1]["region"] is None
    assert result.complete
    assert "country" in result.warnings[0]


def test_region_lookup_requires_exact_city_id():
    def respond(method, params):
        if method == "groups.search":
            return {"count": 1, "items": [{"id": 10}]}
        if method == "groups.getMembers":
            return {"count": 2, "items": [1, 2]}
        if method == "users.get":
            return [
                {"id": 1, "city": {"id": 555, "title": "Город"}, "country": {"id": 1, "title": "Россия"}},
                {"id": 2, "city": {"id": 556, "title": "Город"}, "country": {"id": 1, "title": "Россия"}},
            ]
        if method == "database.getCities":
            return {"items": [{"id": 555, "title": "Город", "region": "Верный регион"},
                              {"id": 999, "title": "Город", "region": "Чужой регион"}]}
        raise AssertionError(method)

    result = VkCollector(settings(), StubVk(respond)).collect("музыка", ["groups"])
    assert result.users[1]["region"] == "Верный регион"
    assert result.users[2]["region"] is None
    assert result.users[1]["country_code"] == "RUS"


def test_demo_profiles_are_stable_across_queries_and_sources():
    collector = DemoCollector()
    first = collector.collect("музыка", ["groups", "likes", "reposts"])
    second = collector.collect("спорт", ["groups", "likes", "reposts"])
    common = set(first.users) & set(second.users)
    assert common
    for uid in common:
        assert first.users[uid] == second.users[uid]
    assert len(first.users) < sum(first.source_stats.values())
    assert first == collector.collect("музыка", ["groups", "likes", "reposts"])


def test_expired_oauth_token_is_refreshed_once_without_leaking_credentials():
    class Provider:
        refreshes = 0

        def access_token(self, deadline=None):
            return "old-access"

        def refresh_after_rejection(self, token, deadline=None):
            assert token == "old-access"
            self.refreshes += 1
            return "new-access"

    provider = Provider()
    calls = []

    def respond(request):
        calls.append(request)
        assert "access" not in str(request.url)
        if request.headers["authorization"] == "Bearer old-access":
            return httpx.Response(200, json={"error": {"error_code": 5}})
        assert request.headers["authorization"] == "Bearer new-access"
        return httpx.Response(200, json={"response": {"items": [], "count": 0}})

    client = VkClient(settings(), httpx.Client(transport=httpx.MockTransport(respond)), token_provider=provider)
    assert client.call("groups.search", deadline=time.monotonic() + 5)["items"] == []
    assert provider.refreshes == 1 and len(calls) == 2


def test_repeated_auth_rejection_does_not_loop_refresh():
    class Provider:
        refreshes = 0

        def access_token(self, deadline=None):
            return "old"

        def refresh_after_rejection(self, token, deadline=None):
            self.refreshes += 1
            return "new"

    provider = Provider()
    client = VkClient(settings(), httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"error": {"error_code": 5}})
    )), token_provider=provider)
    with pytest.raises(VkApiError) as error:
        client.call("groups.search", deadline=time.monotonic() + 5)
    assert error.value.code == 5 and provider.refreshes == 1
