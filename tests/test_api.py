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
