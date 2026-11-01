import hashlib
import json

from redis import Redis
from redis.exceptions import RedisError


class QueryCache:
    """Redis ускоряет подсказки и поиск ID; PostgreSQL проверяет свежесть данных."""

    def __init__(self, url: str):
        self.client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=0.5, socket_timeout=0.5)

    def get(self, key):
        try:
            value = self.client.get("audience:v1:" + key)
            return json.loads(value) if value else None
        except (RedisError, ValueError, TypeError):
            return None

    def set(self, key, value, ttl=300):
        try:
            self.client.setex("audience:v1:" + key, max(1, int(ttl)), json.dumps(value, ensure_ascii=False))
        except RedisError:
            pass

    def healthy(self):
        try:
            return bool(self.client.ping())
        except RedisError:
            return False

    def close(self):
        self.client.close()


def suggestion_key(mode, prefix):
    return "suggestions:" + mode + ":" + hashlib.sha256(prefix.encode()).hexdigest()
