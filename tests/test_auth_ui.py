"""The UI must not confuse demo data, login hints or stored credentials with VK access."""

from pathlib import Path

import pytest
import requests
import streamlit as st
from streamlit.testing.v1 import AppTest


APP = Path(__file__).resolve().parents[1] / "frontend" / "app.py"


class Response:
    def __init__(self, body, ok=True):
        self.body = body
        self.ok = ok

    def json(self):
        return self.body


@pytest.fixture
def ui(monkeypatch):
    st.cache_data.clear()
    state = {
        "status": {
            "enabled": True, "configured": False, "connected": False, "source": "none",
            "app_id": "54871270", "user_id": None, "reauthorization_required": False,
            "login_url": "http://localhost/auth/vk/start",
        },
        "calls": [],
        "configure_ok": True,
        "status_unavailable": False,
    }

    def request(method, url, **kwargs):
        path = url.split("localhost:8000", 1)[-1]
        state["calls"].append((method, path, kwargs))
        if path == "/health":
            return Response({"status": "ok", "mode": "demo"})
        if path == "/api/vk/status":
            if state["status_unavailable"]:
                raise requests.ConnectionError("temporary backend outage")
            return Response(dict(state["status"]))
        if path == "/api/suggestions":
            return Response({"suggestions": ["Фотография"]})
        if path == "/api/vk/configure":
            if not state["configure_ok"]:
                return Response({"detail": "Не удалось сохранить ключ. Повторите попытку."}, ok=False)
            state["status"]["configured"] = True
            return Response({"ok": True})
        if path == "/api/vk/disconnect":
            state["status"].update(connected=False, source="none", user_id=None)
            return Response({"ok": True, "detail": "Подключение отключено."})
        if path == "/api/search":
            return Response({
                "query": kwargs["json"]["query"], "mode": kwargs["json"]["data_mode"],
                "modes": ["groups"], "collected_at": "2026-11-02T10:00:00Z",
                "expires_at": "2026-11-03T10:00:00Z", "total_users": 0, "coverage": {},
                "complete": True, "cache_hit": False,
            })
        raise AssertionError(f"Unexpected UI request: {method} {path}")

    monkeypatch.setattr(requests, "request", request)
    state["app"] = AppTest.from_file(str(APP), default_timeout=15)
    yield state
    st.cache_data.clear()


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_login_url_flag_does_not_grant_access_and_demo_remains_available(ui):
    app = ui["app"]
    app.query_params["vk_login"] = "success"
    app.run()
    assert not app.exception
    assert app.radio(key="data_mode").value == "demo"
    assert not button(app, "Найти аудиторию").disabled
    app.radio(key="data_mode").set_value("vk").run()
    assert not app.exception
    assert button(app, "Найти аудиторию").disabled
    assert not any(method == "POST" for method, _, _ in ui["calls"])
    assert "vk_login" not in app.query_params


@pytest.mark.parametrize("success", [True, False])
def test_service_key_is_sent_only_to_configuration_and_cleared_even_on_failure(ui, success):
    ui["configure_ok"] = success
    app = ui["app"].run()
    app.text_input(key="vk_service_token_input").set_value("test-service-key-never-display")
    button(app, "Сохранить ключ").click().run()
    assert not app.exception
    posts = [(path, kwargs) for method, path, kwargs in ui["calls"] if method == "POST"]
    assert len(posts) == 1
    path, kwargs = posts[0]
    assert path == "/api/vk/configure"
    assert kwargs["json"] == {"service_token": "test-service-key-never-display"}
    assert kwargs["headers"]["X-Requested-With"] == "AudienceDashboard"
    assert app.text_input(key="vk_service_token_input").value == ""
    assert app.session_state["vk_service_token_input"] == ""
    for kind in (app.markdown, app.caption, app.success, app.error, app.warning):
        assert all("test-service-key-never-display" not in item.value for item in kind)


def test_connected_login_selects_vk_but_disconnect_removes_the_previous_result(ui):
    ui["status"].update(configured=True, connected=True, source="oauth", user_id=123)
    app = ui["app"]
    app.query_params["vk_login"] = "success"
    app.run()
    assert not app.exception
    assert app.radio(key="data_mode").value == "vk"
    app.selectbox(key="search_topic").select("Фотография")
    button(app, "Найти аудиторию").click().run()
    assert not app.exception
    assert app.session_state["dashboard"]["mode"] == "vk"
    search = next(kwargs for method, path, kwargs in ui["calls"] if path == "/api/search")
    assert search["json"]["data_mode"] == "vk"
    button(app, "Отключить VK").click().run()
    assert not app.exception
    assert "dashboard" not in app.session_state
    assert button(app, "Найти аудиторию").disabled
    app.radio(key="data_mode").set_value("demo").run()
    assert not app.exception
    assert not button(app, "Найти аудиторию").disabled
    suggestions = [kwargs["params"]["data_mode"] for _, path, kwargs in ui["calls"] if path == "/api/suggestions"]
    assert "vk" in suggestions and "demo" in suggestions


def test_switching_data_source_removes_results_without_starting_another_search(ui):
    ui["status"].update(configured=True, connected=True, source="oauth", user_id=123)
    app = ui["app"].run()
    app.selectbox(key="search_topic").select("Фотография")
    button(app, "Найти аудиторию").click().run()
    assert app.session_state["dashboard"]["mode"] == "demo"
    app.radio(key="data_mode").set_value("vk").run()
    assert not app.exception
    assert "dashboard" not in app.session_state
    assert app.selectbox(key="search_topic").value is None
    assert sum(path == "/api/search" for _, path, _ in ui["calls"]) == 1


def test_status_failure_preserves_last_successful_result_and_blocks_new_vk_search(ui):
    ui["status"].update(configured=True, connected=True, source="oauth", user_id=123)
    app = ui["app"]
    app.query_params["vk_login"] = "success"
    app.run()
    app.selectbox(key="search_topic").select("Фотография")
    button(app, "Найти аудиторию").click().run()
    saved = app.session_state["dashboard"]
    identity = app.session_state["vk_connection_identity"]
    ui["status_unavailable"] = True
    app.run()
    assert not app.exception
    assert app.session_state["dashboard"] == saved
    assert app.session_state["vk_connection_identity"] == identity
    assert button(app, "Найти аудиторию").disabled
    assert any(item.value == "Аудитория: Фотография" for item in app.subheader)
    ui["status_unavailable"] = False
    app.run()
    assert not app.exception
    assert app.session_state["dashboard"] == saved
    assert not button(app, "Найти аудиторию").disabled
    assert sum(path == "/api/search" for _, path, _ in ui["calls"]) == 1
