import hashlib
import json
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://audience:audience@postgres:5432/audience"
    redis_url: str = "redis://redis:6379/0"
    data_mode: Literal["demo", "vk"] = "demo"
    vk_token: SecretStr = SecretStr("")
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
    def validate_token(self):
        if self.data_mode == "vk" and not self.vk_token.get_secret_value().strip():
            raise ValueError("Для DATA_MODE=vk заполните VK_TOKEN в локальном .env")
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
