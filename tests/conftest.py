from datetime import datetime, timezone

import fakeredis
import pytest

from app.cache import QueryCache
from app.collectors import Collection
from app.config import Settings
from app.database import init_database, make_database
from app.service import SearchService


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class StubCollector:
    def __init__(self):
        self.calls = 0
        self.error = None
        self.result = Collection(
            users={1: {"sex": 1, "bdate": "1.1.2000", "city": {"id": 1, "title": "Москва"},
                       "country_title": "Россия", "country_code": "RUS", "region": "Москва"},
                   2: {"sex": 2, "bdate": "5.4"}},
            groups={10: {"name": "Книги", "screen_name": "books", "members_count": 500}},
            memberships={(10, 1), (10, 2)},
            evidence={(1, "groups", "10"), (2, "groups", "10"), (1, "likes", "-10_1")},
        )

    def collect(self, _query, _modes):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def cache():
    cache = QueryCache("redis://localhost:1")
    cache.client = fakeredis.FakeRedis(decode_responses=True)
    return cache


@pytest.fixture
def service(tmp_path, cache):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/test.sqlite", data_mode="demo", _env_file=None)
    engine, sessions = make_database(settings.database_url)
    init_database(engine)
    service = SearchService(settings, sessions, StubCollector(), cache, now=Clock())
    yield service
    engine.dispose()
