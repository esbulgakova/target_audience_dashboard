"""Security and refresh invariants; all VK responses are fake."""

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import stat
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import h11
import httpx
import pytest

from app.auth import AuthError, COOKIE, VkAuth


HEADERS = {"X-Requested-With": "AudienceDashboard"}
VERIFIER = "A" * 64
SERVICE = "private-service-credential"
ACCESS = "private-access-credential"
REFRESH = "private-refresh-credential"


def settings(tmp_path, **kwargs):
    return SimpleNamespace(**{
        "vk_app_id": "54871270", "vk_service_token": SERVICE,
        "vk_redirect_uri": "http://localhost", "frontend_public_url": "http://localhost:8501",
        "vk_auth_file": str(tmp_path / "auth-state" / "vk-auth.json"),
        "vk_timeout_seconds": 3, "vk_token": "", **kwargs,
    })


def token_response(request, access=ACCESS, refresh=REFRESH, **kwargs):
    body = parse_qs(request.content.decode())
    assert SERVICE not in str(request.url) and REFRESH not in str(request.url)
    return httpx.Response(200, json={
        "state": body["state"][0], "access_token": access, "refresh_token": refresh,
        "user_id": 123, "expires_in": 3600, "id_token": "unused-id-secret", **kwargs,
    })


def harness(tmp_path, responder=token_response, **kwargs):
    config = settings(tmp_path, **kwargs)
    auth = VkAuth(config, httpx.Client(transport=httpx.MockTransport(responder)))
    app = FastAPI()
    app.include_router(auth.router)
    return auth, TestClient(app, base_url="http://localhost"), config


def start(client):
    page = client.get("/auth/vk/start")
    assert page.status_code == 200
    assert "HttpOnly" in page.headers["set-cookie"]
    assert "crypto.getRandomValues" in page.text and "crypto.subtle.digest" in page.text
    challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
    prepared = client.post("/auth/vk/prepare", json={"code_verifier": VERIFIER, "code_challenge": challenge}, headers=HEADERS)
    assert prepared.status_code == 200
    url = prepared.json()["authorize_url"]
    return parse_qs(urlsplit(url).query)


def login(client, payload=False):
    params = start(client)
    callback = {"code": "private-code", "device_id": "device-123", "state": params["state"][0]}
    response = client.get("/", params={"payload": json.dumps({"type": "code_v2", **callback})}
                          if payload else callback, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response


@pytest.mark.parametrize("payload", [False, True])
def test_browser_pkce_exchange_and_private_persistence(tmp_path, payload):
    calls = []

    def respond(request):
        body = parse_qs(request.content.decode())
        calls.append(body)
        assert body["code_verifier"] == [VERIFIER]
        assert body["service_token"] == [SERVICE]
        assert body["client_id"] == ["54871270"]
        assert body["redirect_uri"] == ["http://localhost"]
        return token_response(request)

    auth, client, config = harness(tmp_path, respond)
    params = start(client)
    expected = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
    assert params["code_challenge"] == [expected]
    assert params["scope"] == ["vkid.personal_info"]
    callback = {"code": "private-code", "device_id": "device-123", "state": params["state"][0]}
    response = client.get("/", params={"payload": json.dumps(callback)} if payload else callback,
                          follow_redirects=False)
    assert response.status_code == 303 and len(calls) == 1
    assert response.headers["location"] == "http://localhost:8501?vk_login=success"
    status = client.get("/api/vk/status")
    assert status.json()["connected"] and status.json()["user_id"] == 123
    assert COOKIE not in client.cookies
    for secret in (SERVICE, ACCESS, REFRESH, "private-code", "unused-id-secret"):
        assert secret not in response.text + str(response.headers) + status.text
    path = Path(config.vk_auth_file)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert "unused-id-secret" not in path.read_text()
    reloaded = VkAuth(config)
    assert reloaded.access_token() == ACCESS
    assert reloaded.cache_partition() == "oauth:54871270:123"
    reloaded.close()


def test_callback_rejects_cross_browser_state_and_replay_before_network(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return token_response(request)

    auth, client, _ = harness(tmp_path, respond)
    params = start(client)
    binding = client.cookies.get(COOKIE)
    query = {"code": "private-code", "state": params["state"][0], "device_id": "device-123"}
    other = TestClient(client.app, base_url="http://localhost")
    assert other.get("/", params=query).status_code == 400
    assert calls == []
    bad = {**query, "state": "incorrect-state"}
    assert client.get("/", params=bad).status_code == 400
    assert calls == []
    client.cookies.set(COOKIE, binding)
    assert client.get("/", params=query, follow_redirects=False).status_code == 303
    client.cookies.set(COOKIE, binding)
    assert client.get("/", params=query).status_code == 400
    assert len(calls) == 1


def test_expired_state_duplicate_and_conflicting_callback_are_rejected(tmp_path):
    auth, client, _ = harness(tmp_path, lambda request: pytest.fail("Invalid callback reached VK"))
    params = start(client)
    state = params["state"][0]
    assert client.get("/", params=[("state", state), ("state", state)]).status_code == 400
    assert client.get("/", params={"state": state, "payload": json.dumps({"state": "other"})}).status_code == 400
    start(client)
    auth._now = lambda: 10**12
    assert client.get("/", params={"state": state, "code": "secret", "device_id": "device"}).status_code == 400


def test_configure_and_prepare_require_browser_csrf_protection(tmp_path):
    auth, client, _ = harness(tmp_path, vk_service_token="")
    assert client.get("/auth/vk/start").status_code == 400
    assert client.post("/api/vk/configure", json={"service_token": SERVICE}).status_code == 403
    assert client.post("/api/vk/configure", json={"service_token": SERVICE},
                       headers={**HEADERS, "Origin": "https://attacker.example"}).status_code == 403
    result = client.post("/api/vk/configure", json={"service_token": SERVICE}, headers=HEADERS)
    assert result.status_code == 200 and result.json()["configured"]
    assert SERVICE not in result.text
    client.get("/auth/vk/start")
    assert client.post("/auth/vk/prepare", json={"code_verifier": VERIFIER}).status_code == 403
    assert client.post("/auth/vk/prepare", json={"code_verifier": VERIFIER},
                       headers={**HEADERS, "Origin": "null"}).status_code == 403
    assert client.post("/auth/vk/prepare", json={"code_verifier": "short"}, headers=HEADERS).status_code == 400
    assert client.post("/auth/vk/prepare", json={"code_verifier": VERIFIER, "code_challenge": "wrong"}, headers=HEADERS).status_code == 400
    invalid = client.post("/api/vk/configure", content="invalid-sensitive-json", headers={**HEADERS, "Content-Type": "application/json"})
    assert invalid.status_code == 400 and "invalid-sensitive-json" not in invalid.text


def test_single_use_refresh_is_serialized_and_survives_restart(tmp_path):
    refresh_calls = []

    def respond(request):
        body = parse_qs(request.content.decode())
        if body["grant_type"] == ["refresh_token"]:
            assert body["refresh_token"] == [REFRESH]
            refresh_calls.append(body)
            return token_response(request, access="rotated-access", refresh="rotated-refresh")
        return token_response(request)

    auth, client, config = harness(tmp_path, respond)
    login(client)
    expired_at = auth._record["tokens"]["expires_at"]
    auth._now = lambda: expired_at
    with ThreadPoolExecutor(max_workers=4) as executor:
        tokens = list(executor.map(lambda _: auth.access_token(), range(4)))
    assert tokens == ["rotated-access"] * 4 and len(refresh_calls) == 1
    assert auth.refresh_after_rejection(ACCESS) == "rotated-access"
    assert len(refresh_calls) == 1
    reloaded = VkAuth(config)
    assert reloaded.access_token() == "rotated-access"
    assert reloaded._record["tokens"]["refresh_token"] == "rotated-refresh"
    reloaded.close()


@pytest.mark.parametrize("failure", ["timeout", "invalid-state", "invalid-grant"])
def test_ambiguous_refresh_is_never_reused_even_after_restart(tmp_path, failure):
    calls = []

    def respond(request):
        body = parse_qs(request.content.decode())
        if body["grant_type"] == ["refresh_token"]:
            calls.append(request)
            if failure == "timeout":
                raise httpx.ReadTimeout("private-refresh-credential", request=request)
            if failure == "invalid-state":
                return token_response(request, state="wrong")
            return httpx.Response(400, json={"error": "private-refresh-credential"})
        return token_response(request)

    auth, client, config = harness(tmp_path, respond, vk_token="legacy-do-not-fallback")
    login(client)
    expires = auth._record["tokens"]["expires_at"]
    auth._now = lambda: expires
    for _ in range(2):
        with pytest.raises(AuthError) as caught:
            auth.access_token()
        assert REFRESH not in str(caught.value)
    assert len(calls) == 1
    assert auth.status()["reauthorization_required"] and not auth.status()["connected"]
    reloaded = VkAuth(config, httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("Reused refresh"))))
    with pytest.raises(AuthError):
        reloaded.access_token()
    assert not reloaded.status()["connected"]


def test_crash_marker_prevents_reusing_persisted_refresh(tmp_path):
    auth, client, config = harness(tmp_path)
    login(client)
    auth._record["tokens"]["refresh_in_flight"] = True
    auth._persist()
    restarted = VkAuth(config, httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("Reused refresh"))))
    with pytest.raises(AuthError):
        restarted.access_token()


def test_configuration_changes_do_not_reuse_another_apps_tokens(tmp_path):
    auth, client, config = harness(tmp_path)
    login(client)
    config.vk_app_id = "another-app"
    restarted = VkAuth(config)
    assert not restarted.status()["connected"]
    with pytest.raises(AuthError):
        restarted.access_token()
    restarted.close()


def test_remote_errors_and_invalid_token_responses_never_expose_secrets(tmp_path):
    auth, client, _ = harness(tmp_path, lambda request: httpx.Response(200, json={
        "error": "VK rejected", "error_description": ACCESS + SERVICE + REFRESH,
    }))
    params = start(client)
    response = client.get("/", params={"state": params["state"][0], "code": "private-code", "device_id": "device"})
    assert response.status_code == 502
    for secret in (ACCESS, REFRESH, SERVICE, "private-code"):
        assert secret not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("remote_success", [False, True])
def test_disconnect_clears_local_tokens_and_reports_remote_result(tmp_path, remote_success):
    def respond(request):
        if request.url.path.endswith("logout"):
            body = parse_qs(request.content.decode())
            assert body["access_token"] == [ACCESS]
            return httpx.Response(200 if remote_success else 503, json={"response": 1})
        return token_response(request)

    auth, client, config = harness(tmp_path, respond, vk_token="legacy-do-not-fallback")
    login(client)
    assert client.post("/api/vk/disconnect").status_code == 403
    result = client.post("/api/vk/disconnect", headers=HEADERS)
    assert result.status_code == 200 and result.json()["remote_revoked"] == remote_success
    assert not auth.status()["connected"]
    assert auth.status()["configured"]
    with pytest.raises(AuthError):
        auth.access_token()
    reloaded = VkAuth(config)
    assert not reloaded.status()["connected"]
    reloaded.close()


def test_connection_change_is_rejected_during_collection(tmp_path):
    auth, client, _ = harness(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def collect():
        with auth.connection_lock:
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=collect)
    worker.start()
    assert entered.wait(2)
    try:
        result = client.post("/api/vk/configure", json={"service_token": SERVICE}, headers=HEADERS)
        assert result.status_code == 409
        assert client.get("/api/vk/status").status_code == 200
    finally:
        release.set()
        worker.join()


def test_unicode_callback_state_and_duplicate_payload_are_safe(tmp_path):
    auth, client, _ = harness(tmp_path, lambda request: pytest.fail("Bad callback reached VK"))
    start(client)
    result = client.get("/", params={"state": "неправильный-state", "code": "secret", "device_id": "device"})
    assert result.status_code == 400
    assert "неправильный" not in result.text
    result = client.get("/", params={"payload": '{"state":"one","state":"two"}'})
    assert result.status_code == 400


def test_unicode_token_response_state_does_not_crash(tmp_path):
    auth, client, _ = harness(tmp_path, lambda request: token_response(request, state="неправильный"))
    params = start(client)
    result = client.get("/", params={"state": params["state"][0], "code": "secret", "device_id": "device"})
    assert result.status_code == 502
    assert "неправильный" not in result.text


def test_manual_disconnect_does_not_claim_remote_revocation(tmp_path):
    auth, client, _ = harness(tmp_path, vk_token="legacy-secret")
    result = client.post("/api/vk/disconnect", headers=HEADERS).json()
    assert not result["remote_revoked"]
    assert ".env" in result["detail"]
    assert not auth.status()["connected"]


def test_manual_cache_partition_is_stable_but_changes_with_token(tmp_path):
    first = VkAuth(settings(tmp_path, vk_token="legacy-token-one"))
    same = VkAuth(settings(tmp_path, vk_token="legacy-token-one"))
    other = VkAuth(settings(tmp_path, vk_token="legacy-token-two"))
    try:
        assert first.cache_partition() == same.cache_partition()
        assert first.cache_partition() != other.cache_partition()
        assert "legacy-token-one" not in first.cache_partition()
        assert first.cache_partition() not in json.dumps(first.status())
    finally:
        first.close()
        same.close()
        other.close()


def test_auth_headers_are_valid_for_real_http_transport(tmp_path):
    auth, client, _ = harness(tmp_path)
    response = client.get("/api/vk/status")
    assert response.status_code == 200
    # ASGI TestClient accepts whitespace which Uvicorn's actual HTTP encoder rejects.
    h11.Response(status_code=response.status_code, headers=response.headers.raw)
    csp = response.headers["content-security-policy"]
    assert csp == csp.strip()
