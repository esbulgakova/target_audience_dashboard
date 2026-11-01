"""Uses a fresh schema in a dedicated PostgreSQL test database; never drops public."""
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.cache import QueryCache
from app.collectors import Collection
from app.config import Settings
from app.database import init_database
from app.models import SearchUser, User
from app.service import CollectionBusy, SearchService
from conftest import Clock, StubCollector


@pytest.fixture
def real_service():
    url = os.environ.get("TEST_DATABASE_URL")
    redis_url = os.environ.get("TEST_REDIS_URL")
    if not url or not redis_url:
        pytest.skip("Для настоящих PostgreSQL/Redis: docker compose --profile test run --build --rm tests")
    schema = "test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    cache = QueryCache(redis_url)
    assert cache.healthy()
    init_database(engine)
    settings = Settings(database_url=url, data_mode="demo", _env_file=None)
    service = SearchService(settings, sessionmaker(engine, expire_on_commit=False), StubCollector(), cache, Clock())
    try:
        yield service
    finally:
        engine.dispose()
        cache.close()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.integration
def test_postgres_upsert_redis_and_transaction_lock(real_service):
    service = real_service
    first = service.search("Книги", ["groups"])
    assert first["total_users"] == 2
    assert service.cache.get("query:" + service.query_key("Книги", ["groups"])) is not None
    assert service.search("книги", ["groups"])["cache_hit"]
    # Second process/service is excluded by the database advisory lock.
    with service.sessions.begin() as session:
        session.execute(text("SELECT pg_advisory_xact_lock(724191854)"))
        with pytest.raises(CollectionBusy):
            service.search("Велосипеды", ["groups"])
    service.now.value += timedelta(days=2)
    service.collector.result = Collection(users={1: {"sex": 2}, 3: {"sex": 1}})
    assert service.search("книги", ["groups"])["total_users"] == 2
    with service.sessions() as session:
        assert session.get(User, ("demo", 1)).sex == 2
        assert set(session.scalars(select(SearchUser.user_id))) == {1, 3}
