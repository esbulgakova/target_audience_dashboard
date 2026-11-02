"""VK ID for one local operator: browser PKCE, server tokens and single-use refresh."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import httpx

from .auth_pages import error_page, login_page


COOKIE = "audience_vk_login"
AUTH_ENDPOINT = "https://id.vk.ru/oauth2/auth"
SESSION_SECONDS = 600
RECONNECT = "Подключение VK нужно обновить. Войдите через VK ID ещё раз."


class AuthError(RuntimeError):
    def __init__(self, message: str, status_code: int = 401):
        self.status_code = status_code
        super().__init__(message)


def _secret(value):
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value or "")


def _same(left, right):
    # compare_digest(str, str) rejects Unicode from untrusted callback input.
    return hmac.compare_digest(left.encode("utf-8", "surrogatepass"), right.encode("utf-8", "surrogatepass"))


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _origin(url):
    parsed = urlsplit(str(url))
    return f"{parsed.scheme}://{parsed.netloc}"


def _safe_response(response, nonce=None):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'; "
        + (f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'" if nonce else "")
    ).rstrip()
    return response


class VkAuth:
    """One process/worker; connection_lock also guards an entire audience collection."""

    def __init__(self, settings, client=None):
        self.settings = settings
        self.app_id = str(settings.vk_app_id or "")
        self.redirect_uri = settings.vk_redirect_uri
        self.frontend_url = settings.frontend_public_url
        self.path = Path(settings.vk_auth_file)
        self.client = client or httpx.Client(follow_redirects=False)
        self.connection_lock = threading.RLock()
        self._lock = threading.RLock()
        self._now = time.time
        self._pending = {}
        self._record = self._load()
        self.router = self._make_router()

    def close(self):
        self.client.close()

    def _empty_record(self):
        return {"app_id": self.app_id, "redirect_uri": self.redirect_uri,
                "service_token": "", "tokens": None, "oauth_used": False,
                "reauthorization_required": False}

    def _load(self):
        record = self._empty_record()
        try:
            saved = json.loads(self.path.read_text())
            if not isinstance(saved, dict):
                raise ValueError
            if saved.get("app_id") == self.app_id and saved.get("redirect_uri") == self.redirect_uri:
                record.update({key: saved[key] for key in record if key in saved})
                tokens = record.get("tokens")
                if tokens is not None:
                    if not isinstance(tokens, dict):
                        raise ValueError
                    for name in ("access_token", "refresh_token", "device_id"):
                        if not isinstance(tokens.get(name), str) or not tokens[name]:
                            raise ValueError
                    if not isinstance(tokens.get("expires_at"), (int, float)) or not math.isfinite(tokens["expires_at"]):
                        raise ValueError
                    if not isinstance(tokens.get("user_id"), int) or isinstance(tokens["user_id"], bool) or tokens["user_id"] <= 0:
                        raise ValueError
                    if tokens.get("refresh_in_flight"):
                        record["reauthorization_required"] = True
            else:
                # An app's credentials must never be reused by a different application.
                record["oauth_used"] = bool(saved.get("oauth_used"))
                record["reauthorization_required"] = record["oauth_used"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            record["oauth_used"] = True
            record["reauthorization_required"] = True
        return record

    def _persist(self):
        """Replace the whole token pair atomically, with private filesystem permissions."""
        temp_path = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            fd, temp_path = tempfile.mkstemp(prefix=".vk-auth-", dir=self.path.parent)
            with os.fdopen(fd, "w") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(self._record, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            raise AuthError("Не удалось сохранить подключение VK. Проверьте доступ к хранилищу backend.", 503) from None
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _service_token(self):
        value = self._record.get("service_token") or _secret(self.settings.vk_service_token)
        return value if isinstance(value, str) else ""

    def _manual_token(self):
        return _secret(self.settings.vk_token).strip()

    def status(self):
        with self._lock:
            tokens = self._record.get("tokens")
            oauth = bool(tokens or self._record.get("oauth_used"))
            reconnect = bool(self._record.get("reauthorization_required"))
            manual = bool(self._manual_token()) and not oauth
            expires = tokens.get("expires_at") if tokens else None
            if tokens and self._now() >= tokens.get("refresh_expires_at", 0):
                reconnect = True
            return {
                "app_id": self.app_id, "enabled": bool(self.app_id),
                "configured": bool(self.app_id and self._service_token()),
                "connected": manual or bool(tokens and not reconnect and self._service_token()),
                "source": "oauth" if oauth else "manual" if manual else "none",
                "user_id": tokens.get("user_id") if tokens else None,
                "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat() if expires else None,
                "reauthorization_required": reconnect,
                "login_url": _origin(self.redirect_uri) + "/auth/vk/start",
            }

    def cache_partition(self):
        status = self.status()
        if status["source"] == "oauth":
            return f"oauth:{self.app_id}:{status['user_id'] or 'disconnected'}"
        if status["source"] == "manual":
            # A legacy token has no verified user ID; isolate its cache without exposing the key.
            fingerprint = hashlib.sha256(self._manual_token().encode()).hexdigest()
            return "manual:" + fingerprint
        return "disconnected"

    def configure(self, service_token):
        if not self.app_id:
            raise AuthError("Сначала задайте VK_APP_ID в настройках сервиса.", 400)
        if (not isinstance(service_token, str) or not 10 <= len(service_token.strip()) <= 8192
                or not service_token.strip().isascii() or any(char.isspace() for char in service_token.strip())):
            raise AuthError("Введите сервисный ключ приложения из кабинета VK ID.", 400)
        with self._connection_change(), self._lock:
            self._record["service_token"] = service_token.strip()
            self._record["tokens"] = None
            self._record["reauthorization_required"] = bool(self._record["oauth_used"])
            self._pending.clear()
            self._persist()
        return {"ok": True, **self.status()}

    @contextmanager
    def _connection_change(self):
        if not self.connection_lock.acquire(blocking=False):
            raise AuthError("Сейчас идёт сбор аудитории. Дождитесь его окончания и повторите подключение.", 409)
        try:
            yield
        finally:
            self.connection_lock.release()

    def _require_configured(self):
        if not self.app_id or not self._service_token():
            raise AuthError("Сначала сохраните сервисный ключ конфиденциального приложения VK ID в дашборде.", 400)

    def _timeout(self, deadline):
        timeout = self.settings.vk_timeout_seconds
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise AuthError("Истекло время ожидания VK. Повторите поиск.", 504)
        return timeout

    def _post(self, url, data, deadline=None):
        try:
            response = self.client.post(url, data=data, timeout=self._timeout(deadline))
            payload = response.json()
        except (httpx.RequestError, ValueError):
            raise AuthError("VK ID не подтвердил операцию. Проверьте соединение и войдите снова.", 502) from None
        if response.status_code != 200 or not isinstance(payload, dict) or "error" in payload:
            raise AuthError("VK ID отклонил запрос. Проверьте сервисный ключ, конфиденциальный тип приложения и разрешённый внешний IP сервера.", 502)
        return payload

    def _parse_tokens(self, payload, state, device_id, expected_user=None):
        if not isinstance(payload.get("state"), str) or not _same(payload["state"], state):
            raise AuthError("VK ID вернул ответ, который не соответствует запросу. Войдите снова.", 502)
        for key in ("access_token", "refresh_token"):
            if (not isinstance(payload.get(key), str) or not 1 <= len(payload[key]) <= 16384
                    or not payload[key].isascii() or any(char.isspace() for char in payload[key])):
                raise AuthError("VK ID вернул неполный набор токенов. Войдите снова.", 502)
        expiry = payload.get("expires_in")
        user_id = payload.get("user_id")
        if (not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
                or not math.isfinite(expiry) or not 0 < expiry <= 86400
                or not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0
                or (expected_user is not None and expected_user != user_id)):
            raise AuthError("VK ID вернул некорректные параметры подключения. Войдите снова.", 502)
        return {
            "access_token": payload["access_token"], "refresh_token": payload["refresh_token"],
            "device_id": device_id, "user_id": user_id,
            "expires_at": self._now() + expiry,
            "refresh_expires_at": self._now() + 180 * 86400,
            "refresh_in_flight": False,
        }

    def access_token(self, deadline=None):
        with self._lock:
            if self._record["reauthorization_required"]:
                raise AuthError(RECONNECT)
            tokens = self._record.get("tokens")
            if not tokens:
                if not self._record["oauth_used"] and self._manual_token():
                    return self._manual_token()
                raise AuthError("Подключите аккаунт через кнопку «Войти через VK ID».")
            if tokens["expires_at"] > self._now() + 60:
                return tokens["access_token"]
            return self._refresh(deadline)

    def refresh_after_rejection(self, rejected_token, deadline=None):
        with self._lock:
            tokens = self._record.get("tokens")
            if not tokens or self._record["reauthorization_required"]:
                raise AuthError(RECONNECT)
            if not _same(tokens["access_token"], rejected_token):
                return self.access_token(deadline)
            return self._refresh(deadline)

    def _refresh(self, deadline):
        self._require_configured()
        tokens = self._record["tokens"]
        if tokens.get("refresh_in_flight") or tokens.get("refresh_expires_at", 0) <= self._now():
            self._record["reauthorization_required"] = True
            raise AuthError(RECONNECT)
        # Check deadline before consuming the single-use credential.
        self._timeout(deadline)
        state = secrets.token_urlsafe(32)
        data = {
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": self.app_id, "device_id": tokens["device_id"], "state": state,
            "service_token": self._service_token(),
        }
        # A crash or ambiguous timeout must not cause a restart to reuse this token.
        tokens["refresh_in_flight"] = True
        self._record["reauthorization_required"] = True
        self._persist()
        try:
            payload = self._post(AUTH_ENDPOINT, data, deadline)
            updated = self._parse_tokens(payload, state, tokens["device_id"], tokens["user_id"])
            self._record["tokens"] = updated
            self._record["reauthorization_required"] = False
            self._persist()
        except AuthError:
            self._record["tokens"] = None
            self._record["reauthorization_required"] = True
            self._persist()
            raise AuthError(RECONNECT) from None
        return updated["access_token"]

    def _prune(self):
        self._pending = {key: value for key, value in self._pending.items() if value["expires_at"] > self._now()}
        if len(self._pending) >= 32:
            self._pending.pop(next(iter(self._pending)))

    def begin(self):
        with self._lock:
            self._require_configured()
            self._prune()
            binding = secrets.token_urlsafe(32)
            self._pending[binding] = {"expires_at": self._now() + SESSION_SECONDS, "state": None}
            return binding

    def prepare(self, binding, verifier, browser_challenge=None):
        with self._lock:
            self._require_configured()
            pending = self._pending.get(binding)
            if not pending or pending["expires_at"] <= self._now() or pending["state"]:
                raise AuthError("Страница входа устарела. Откройте её заново.", 400)
            if not isinstance(verifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", verifier):
                raise AuthError("Некорректный запрос входа. Откройте страницу заново.", 400)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
            if browser_challenge is not None and browser_challenge != challenge:
                raise AuthError("Некорректная проверка PKCE. Начните вход заново.", 400)
            state = secrets.token_urlsafe(32)
            pending.update(state=state, verifier=verifier)
            return "https://id.vk.ru/authorize?" + urlencode({
                "response_type": "code", "client_id": self.app_id, "redirect_uri": self.redirect_uri,
                "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                "scope": "vkid.personal_info",
            })

    def exchange(self, binding, callback):
        with self._connection_change(), self._lock:
            self._require_configured()
            pending = self._pending.get(binding)
            state = callback.get("state")
            if (not pending or pending["expires_at"] <= self._now() or not pending.get("state")
                    or not isinstance(state, str) or not _same(pending["state"], state)):
                raise AuthError("Не удалось подтвердить запрос входа. Начните вход заново в том же браузере.", 400)
            self._pending.pop(binding)  # One attempt; an ambiguous code exchange is never retried.
            if "error" in callback:
                raise AuthError("Вход через VK ID отменён или отклонён. Можно попробовать снова.", 400)
            code, device_id = callback.get("code"), callback.get("device_id")
            if (not isinstance(code, str) or not 1 <= len(code) <= 8192
                    or not isinstance(device_id, str) or not 1 <= len(device_id) <= 4096):
                raise AuthError("VK ID не передал код входа или идентификатор устройства. Начните вход заново.", 400)
            payload = self._post(AUTH_ENDPOINT, {
                "grant_type": "authorization_code", "code": code, "code_verifier": pending["verifier"],
                "client_id": self.app_id, "redirect_uri": self.redirect_uri, "device_id": device_id,
                "state": state, "service_token": self._service_token(),
            })
            self._record["tokens"] = self._parse_tokens(payload, state, device_id)
            self._record["oauth_used"] = True
            self._record["reauthorization_required"] = False
            try:
                self._persist()
            except AuthError:
                self._record["tokens"] = None
                self._record["reauthorization_required"] = True
                raise

    def disconnect(self):
        with self._connection_change(), self._lock:
            tokens = self._record.get("tokens")
            manual = bool(self._manual_token()) and not self._record["oauth_used"]
            revoked = False
            if tokens:
                try:
                    result = self._post("https://id.vk.ru/oauth2/logout", {
                        "client_id": self.app_id, "access_token": tokens["access_token"],
                    })
                    revoked = result.get("response") == 1
                except AuthError:
                    revoked = False
            self._record["tokens"] = None
            self._record["oauth_used"] = True  # Explicit disconnect must not reactivate a legacy key.
            self._record["reauthorization_required"] = False
            self._pending.clear()
            self._persist()
            detail = "Подключение удалено." if revoked else (
                "Подключение удалено локально. VK не подтвердил отзыв: завершите сессию приложения в настройках VK ID."
            )
            if manual:
                detail += " Статический VK_TOKEN в .env не изменён; при необходимости удалите его из файла."
            return {"ok": True, "disconnected": True, "remote_revoked": revoked, "detail": detail}

    @staticmethod
    def _guard(request):
        if request.headers.get("X-Requested-With") != "AudienceDashboard":
            raise AuthError("Запрос должен быть отправлен из интерфейса приложения.", 403)
        origin = request.headers.get("origin")
        if origin is not None and origin != _origin(request.base_url):
            raise AuthError("Запрос пришёл с другого сайта.", 403)

    @staticmethod
    async def _body(request):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise AuthError("Ожидается запрос из интерфейса приложения.", 400)
        body = await request.body()
        if len(body) > 16384:
            raise AuthError("Запрос слишком большой.", 400)
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, TypeError):
            raise AuthError("Некорректный запрос.", 400) from None

    @staticmethod
    def _callback_parameters(request):
        pairs = list(request.query_params.multi_items())
        if len(str(request.url.query)) > 20000 or len({key for key, _ in pairs}) != len(pairs):
            raise AuthError("Некорректный ответ входа.", 400)
        direct = dict(pairs)
        if "payload" in direct:
            try:
                nested = json.loads(direct.pop("payload"), object_pairs_hook=_unique_object)
                if not isinstance(nested, dict) or nested.get("type", "code_v2") != "code_v2":
                    raise ValueError
                if any(key in direct and direct[key] != value for key, value in nested.items()):
                    raise ValueError
                direct.update(nested)
            except (ValueError, TypeError):
                raise AuthError("Некорректный ответ входа.", 400) from None
        return direct

    def _make_router(self):
        router = APIRouter()

        def error_json(error):
            return _safe_response(JSONResponse({"detail": str(error)}, status_code=error.status_code))

        @router.get("/api/vk/status")
        def status():
            return _safe_response(JSONResponse(self.status()))

        @router.post("/api/vk/configure")
        async def configure(request: Request):
            try:
                self._guard(request)
                body = await self._body(request)
                return _safe_response(JSONResponse(self.configure(body.get("service_token"))))
            except AuthError as error:
                return error_json(error)

        @router.post("/api/vk/disconnect")
        def disconnect(request: Request):
            try:
                self._guard(request)
                return _safe_response(JSONResponse(self.disconnect()))
            except AuthError as error:
                return error_json(error)

        @router.get("/auth/vk/start")
        def start():
            nonce = secrets.token_urlsafe(24)
            try:
                binding = self.begin()
                response = HTMLResponse(login_page(nonce))
                response.set_cookie(COOKIE, binding, max_age=SESSION_SECONDS, httponly=True,
                                    samesite="lax", secure=self.redirect_uri.startswith("https://"), path="/")
            except AuthError as error:
                response = HTMLResponse(error_page(str(error), self.frontend_url, nonce), status_code=error.status_code)
            return _safe_response(response, nonce)

        @router.post("/auth/vk/prepare")
        async def prepare(request: Request):
            try:
                self._guard(request)
                body = await self._body(request)
                target = self.prepare(request.cookies.get(COOKIE), body.get("code_verifier"), body.get("code_challenge"))
                return _safe_response(JSONResponse({"authorize_url": target}))
            except AuthError as error:
                return error_json(error)

        @router.get("/")
        def callback(request: Request):
            nonce = secrets.token_urlsafe(24)
            if not request.query_params:
                return _safe_response(RedirectResponse(self.frontend_url, status_code=303))
            try:
                parameters = self._callback_parameters(request)
                self.exchange(request.cookies.get(COOKIE), parameters)
                response = RedirectResponse(self.frontend_url + "?vk_login=success", status_code=303)
            except AuthError as error:
                response = HTMLResponse(error_page(str(error), self.frontend_url, nonce), status_code=error.status_code)
            response.delete_cookie(COOKIE, path="/")
            return _safe_response(response, nonce)

        return router
