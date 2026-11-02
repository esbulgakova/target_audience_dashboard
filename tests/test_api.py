import pytest
from fastapi.testclient import TestClient

from app.collectors import CollectionError, DemoCollector
from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path, cache):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/api.sqlite", _env_file=None), cache=cache)
    with TestClient(app) as client:
        yield client


def test_full_demo_api_flow_and_no_personal_data_exposure(client):
    assert client.get("/health").json() == {"status": "ok", "mode": "demo", "redis": "ok"}
    data = client.post("/api/search", json={"query": "Книги", "modes": ["groups", "likes", "reposts"]}).json()
    assert data["mode"] == "demo" and data["total_users"] > 0
    assert "users" not in data and "profiles" not in data
    assert sum(row["count"] for row in data["gender"]) == data["total_users"]
    assert sum(row["count"] for row in data["age"]) == data["total_users"]
    assert sum(row["count"] for row in data["geography"]) == data["total_users"]
    assert client.post("/api/search", json={"query": "книги", "modes": ["reposts", "groups", "likes"]}).json()["cache_hit"]
    assert client.get("/api/suggestions", params={"q": "кн"}).json()["suggestions"] == ["Книги"]


@pytest.mark.parametrize("payload", [{"query": " "}, {"query": "аб", "modes": []}, {"query": "аб", "modes": ["secret"]},
                                     {"query": "!?!"}, {"query": "ﬃ" * 100}, {"query": "a" * 201}])
def test_invalid_search_is_rejected(client, payload):
    assert client.post("/api/search", json=payload).status_code == 422


def test_safe_collector_error_response(client):
    class Failed:
        def collect(self, *args):
            raise CollectionError("Нет доступа к методу VK")
    client.app.state.service.collector = Failed()
    response = client.post("/api/search", json={"query": "Книги"})
    assert response.status_code == 502 and response.json()["detail"] == "Нет доступа к методу VK"


def test_demo_profiles_stable_across_topics():
    a = DemoCollector().collect("книги", ["groups", "likes"])
    b = DemoCollector().collect("велосипеды", ["groups", "likes"])
    for user_id in a.users.keys() & b.users.keys():
        assert a.users[user_id] == b.users[user_id]


def test_mode_selection_keeps_demo_and_vk_queries_separate(tmp_path, cache):
    from conftest import StubCollector
    settings = Settings(database_url=f"sqlite:///{tmp_path}/modes.sqlite", vk_token="test-manual-token",
                        vk_auth_file=str(tmp_path / "auth.json"), _env_file=None)
    app = create_app(settings, cache=cache)
    vk = StubCollector()
    app.state.services["vk"].collector = vk
    with TestClient(app) as client:
        demo = client.post("/api/search", json={"query": "Книги", "data_mode": "demo"}).json()
        real = client.post("/api/search", json={"query": "Книги", "data_mode": "vk"}).json()
        assert demo["mode"] == "demo" and real["mode"] == "vk"
        assert real["total_users"] == 2 and not real["cache_hit"]
        assert client.post("/api/search", json={"query": "КНИГИ", "data_mode": "vk"}).json()["cache_hit"]
        assert vk.calls == 1
        assert client.get("/api/suggestions", params={"data_mode": "vk"}).json()["suggestions"] == ["Книги"]


def test_vk_requires_login_but_demo_still_works(client):
    response = client.post("/api/search", json={"query": "Книги", "data_mode": "vk"})
    assert response.status_code == 401
    assert client.post("/api/search", json={"query": "Книги", "data_mode": "demo"}).status_code == 200
    assert client.post("/api/vk/check").status_code == 403
    assert client.post("/api/vk/check", headers={"X-Requested-With": "AudienceDashboard", "Origin": "https://elsewhere.example"}).status_code == 403
    assert client.get("/health", headers={"host": "malicious.example"}).status_code == 400


def test_vk_query_cache_partition_changes_with_account(service):
    account = ["oauth:123:1"]
    service.credential_partition = lambda: account[0]
    first = service.search("Книги", ["groups"])
    assert not first["cache_hit"]
    account[0] = "oauth:123:2"
    assert not service.search("Книги", ["groups"])["cache_hit"]
    assert service.collector.calls == 2
    account[0] = "oauth:123:1"
    assert service.search("Книги", ["groups"])["cache_hit"]


def test_suggestion_history_is_separate_for_vk_connections(service):
    account = ["oauth:123:1"]
    service.credential_partition = lambda: account[0]
    service.search("Книги", ["groups"])
    assert service.suggestions("") == ["Книги"]
    account[0] = "oauth:123:2"
    assert service.suggestions("") == []
    service.search("Велосипеды", ["groups"])
    assert service.suggestions("") == ["Велосипеды"]
    account[0] = "oauth:123:1"
    assert service.suggestions("") == ["Книги"]
