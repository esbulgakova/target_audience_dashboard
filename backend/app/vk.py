"""Small synchronous VK client: form POSTs keep credentials out of URLs."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx


class VkApiError(RuntimeError):
    """An intentionally sanitized error safe to display in the dashboard."""

    def __init__(self, method: str, message: str, code: int | None = None):
        self.method = method
        self.code = code
        suffix = f" (код VK: {code})" if code is not None else ""
        super().__init__(f"{method}: {message}{suffix}")


ERROR_MESSAGES = {
    5: "токен VK недействителен или истёк. Проверьте VK_TOKEN в .env.",
    6: "VK ограничил частоту запросов. Попробуйте позже.",
    7: "приложению или токену не разрешён этот метод.",
    9: "VK ограничил число однотипных действий. Попробуйте позже.",
    10: "внутренняя ошибка VK. Попробуйте позже.",
    14: "VK требует подтверждения CAPTCHA; автоматический обход не выполняется.",
    15: "доступ к данным закрыт настройками приватности или правами токена.",
    18: "страница удалена или заблокирована.",
    27: "этот метод недоступен с токеном сообщества.",
    28: "этот метод недоступен с сервисным токеном приложения.",
    29: "достигнут лимит VK для этого метода. Попробуйте позже.",
    30: "профиль закрыт.",
    100: "VK отклонил параметры запроса; проверьте доступность метода и версию API.",
    113: "объект VK не найден или недоступен.",
    200: "доступ к сообществу закрыт.",
    203: "доступ к сообществу ограничен.",
}


class VkClient:
    # A single rate limiter for every request handled by this API process.
    # Run one Uvicorn worker (as the supplied Compose configuration does).
    _rate_lock = threading.Lock()
    _next_request_at = 0.0

    def __init__(self, settings: Any, client: httpx.Client | None = None):
        token = settings.vk_token
        self.token = token.get_secret_value() if hasattr(token, "get_secret_value") else str(token or "")
        self.version = settings.vk_api_version
        self.base_url = settings.vk_api_base_url.rstrip("/")
        self.interval = settings.vk_request_interval
        self.timeout = settings.vk_timeout_seconds
        self.client = client or httpx.Client(follow_redirects=False)

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def _remaining(deadline: float, method: str) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VkApiError(method, "превышено время сбора. Уменьшите лимиты поиска.")
        return remaining

    def _wait_turn(self, deadline: float, method: str) -> None:
        remaining = self._remaining(deadline, method)
        if not self._rate_lock.acquire(timeout=remaining):
            raise VkApiError(method, "истекло время ожидания очереди запросов VK.")
        try:
            wait = max(0.0, type(self)._next_request_at - time.monotonic())
            if wait >= self._remaining(deadline, method):
                raise VkApiError(method, "превышено время сбора. Уменьшите лимиты поиска.")
            if wait:
                time.sleep(wait)
            type(self)._next_request_at = time.monotonic() + self.interval
        finally:
            self._rate_lock.release()

    def _backoff(self, attempt: int, deadline: float, method: str) -> None:
        wait = 0.5 * (2**attempt)
        if wait >= self._remaining(deadline, method):
            raise VkApiError(method, "превышено время сбора. Уменьшите лимиты поиска.")
        time.sleep(wait)

    @staticmethod
    def _validate_response(method: str, response: Any) -> None:
        """An invalid payload is a failure, never a successful empty audience."""
        message = "VK вернул данные неожиданного формата. Повторите поиск позже."
        if method == "users.get":
            if not isinstance(response, list) or any(not isinstance(item, dict) for item in response):
                raise VkApiError(method, message)
            return
        list_methods = {
            "groups.search", "groups.getMembers", "newsfeed.search",
            "likes.getList", "database.getCities",
        }
        if method not in list_methods:
            return
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise VkApiError(method, message)
        if "count" in response and (
            not isinstance(response["count"], int) or isinstance(response["count"], bool) or response["count"] < 0
        ):
            raise VkApiError(method, message)
        if method in ("groups.search", "newsfeed.search", "database.getCities"):
            if any(not isinstance(item, dict) for item in response["items"]):
                raise VkApiError(method, message)
        elif any(not isinstance(item, (int, dict)) for item in response["items"]):
            raise VkApiError(method, message)

    def call(self, method: str, *, deadline: float, **params: Any) -> Any:
        if not self.token:
            raise VkApiError(method, "VK_TOKEN не задан. Добавьте токен в .env или установите DATA_MODE=demo.")
        for attempt in range(3):
            self._wait_turn(deadline, method)
            try:
                response = self.client.post(
                    f"{self.base_url}/{method}",
                    data={**params, "access_token": self.token, "v": self.version, "lang": "ru"},
                    timeout=min(self.timeout, self._remaining(deadline, method)),
                )
            except httpx.RequestError:
                if attempt < 2:
                    self._backoff(attempt, deadline, method)
                    continue
                raise VkApiError(method, "не удалось связаться с VK. Проверьте сеть и повторите поиск.") from None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    self._backoff(attempt, deadline, method)
                    continue
                raise VkApiError(method, "VK временно недоступен или ограничил запросы. Попробуйте позже.")
            if response.status_code != 200:
                raise VkApiError(method, f"VK вернул HTTP {response.status_code}; проверьте доступ к API.")
            try:
                payload = response.json()
            except ValueError:
                raise VkApiError(method, "VK вернул некорректный ответ.") from None
            if not isinstance(payload, dict):
                raise VkApiError(method, "VK вернул некорректный ответ.")
            if "error" in payload:
                error = payload["error"]
                code = error.get("error_code") if isinstance(error, dict) else None
                code = code if isinstance(code, int) and not isinstance(code, bool) else None
                if code in (6, 10) and attempt < 2:
                    self._backoff(attempt, deadline, method)
                    continue
                # Never expose error_msg, request_params, request URLs or tokens.
                raise VkApiError(method, ERROR_MESSAGES.get(code, "VK отклонил запрос."), code)
            if "response" not in payload:
                raise VkApiError(method, "в ответе VK отсутствуют данные.")
            self._remaining(deadline, method)
            self._validate_response(method, payload["response"])
            return payload["response"]
        raise VkApiError(method, "не удалось завершить запрос VK.")
