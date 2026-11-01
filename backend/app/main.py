from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .cache import QueryCache
from .config import Settings
from .database import init_database, make_database
from .service import CollectionBusy, SearchService, normalize_query


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    modes: list[Literal["groups", "likes", "reposts"]] = Field(default=["groups"], min_length=1, max_length=3)

    @field_validator("query")
    @classmethod
    def meaningful_query(cls, value):
        if not 2 <= len(normalize_query(value)) <= 200 or not any(char.isalnum() for char in value):
            raise ValueError("Введите тему длиной от 2 до 200 символов")
        return value.strip()


def create_app(settings=None, collector=None, cache=None):
    settings = settings or Settings()
    engine, sessions = make_database(settings.database_url)
    cache = cache or QueryCache(settings.redis_url)
    if collector is None:
        from .collectors import DemoCollector, VkCollector
        collector = DemoCollector() if settings.data_mode == "demo" else VkCollector(settings)
    service = SearchService(settings, sessions, collector, cache)

    @asynccontextmanager
    async def lifespan(app):
        init_database(engine)
        yield
        engine.dispose()
        if hasattr(cache, "close"):
            cache.close()
        if hasattr(collector, "close"):
            collector.close()

    app = FastAPI(title="Аудитория VK — учебный проект", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.state.engine = engine

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request, _error):
        # Не отправляем SQL, пароль подключения и профили пользователей в браузер/лог.
        return JSONResponse(status_code=503, content={"detail": "База данных недоступна. Проверьте контейнер postgres."})

    @app.get("/health")
    def health():
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "mode": settings.data_mode, "redis": "ok" if cache.healthy() else "unavailable"}

    @app.get("/api/suggestions")
    def suggestions(q: str = Query("", max_length=200)):
        return {"suggestions": service.suggestions(q)}

    @app.post("/api/search")
    def search(request: SearchRequest):
        from .collectors import CollectionError
        try:
            return service.search(request.query, request.modes)
        except CollectionBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except CollectionError as error:
            raise HTTPException(status_code=502, detail=str(error)) from None

    return app


# uvicorn app.main:create_app --factory — конфигурация проверяется при старте.
