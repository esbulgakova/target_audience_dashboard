import hashlib
import json
from typing import Literal

from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://audience:audience@postgres:5432/audience"
    redis_url: str = "redis://redis:6379/0"
    data_mode: Literal["demo", "vk"] = "demo"
    vk_token: SecretStr = SecretStr("")
    vk_app_id: str = Field("", pattern=r"^([0-9]+)?$")
    vk_service_token: SecretStr = SecretStr("")
    vk_redirect_uri: str = "http://localhost"
    frontend_public_url: str = "http://localhost:8501"
    vk_auth_file: str = "/app/state/vk-auth.json"
    vk_api_version: str = "5.199"
    vk_api_base_url: str = "https://api.vk.com/method"
    vk_request_interval: float = Field(0.36, ge=0.34)
    vk_timeout_seconds: float = Field(15, gt=0, le=30)
    collection_timeout_seconds: float = Field(240, gt=0, le=280)
    cache_ttl_seconds: int = Field(86400, ge=1, le=86400)
    max_groups: int = Field(3, ge=1, le=20)
    max_members_per_group: int = Field(500, ge=1, le=5000)
    max_posts: int = Field(10, ge=1, le=100)
    max_reactions_per_post: int = Field(200, ge=1, le=1000)
    max_users: int = Field(3000, ge=1, le=10000)
    max_city_lookups: int = Field(20, ge=0, le=100)
    post_search_days: int = Field(30, ge=1, le=365)

    @model_validator(mode="after")
    def validate_local_addresses(self):
        # Этот проект запускается локально; redirect не берётся из запроса браузера.
        if self.vk_redirect_uri not in {"http://localhost", "https://localhost"}:
            raise ValueError("VK_REDIRECT_URI должен быть http://localhost или https://localhost")
        url = urlsplit(self.frontend_public_url)
        if (
            url.scheme not in {"http", "https"} or url.hostname not in {"localhost", "127.0.0.1"}
            or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}
        ):
            raise ValueError("FRONTEND_PUBLIC_URL должен указывать на локальное приложение")
        self.frontend_public_url = self.frontend_public_url.rstrip("/")
        return self

    def policy_key(self) -> str:
        """Настройки сбора входят в ключ, секреты — никогда."""
        policy = {
            name: getattr(self, name)
            for name in (
                "max_groups", "max_members_per_group", "max_posts", "max_reactions_per_post",
                "max_users", "max_city_lookups", "post_search_days", "vk_api_version",
            )
        }
        policy["schema"] = 1
        return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:16]
