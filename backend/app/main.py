from contextlib import asynccontextmanager
import time
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .auth import AuthError, VkAuth
from .cache import QueryCache
from .collectors import CollectionError, DemoCollector, VkCollector
from .config import Settings
from .database import init_database, make_database
from .service import CollectionBusy, SearchService, normalize_query
from .vk import VkApiError, VkClient


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    modes: list[Literal["groups", "likes", "reposts"]] = Field(default=["groups"], min_length=1, max_length=3)
    data_mode: Literal["demo", "vk"] | None = None

    @field_validator("query")
    @classmethod
    def meaningful_query(cls, value):
        if not 2 <= len(normalize_query(value)) <= 200 or not any(char.isalnum() for char in value):
            raise ValueError("Введите тему длиной от 2 до 200 символов")
        return value.strip()


def create_app(settings=None, collector=None, cache=None, auth=None):
    settings = settings or Settings()
    engine, sessions = make_database(settings.database_url)
    cache = cache or QueryCache(settings.redis_url)
    auth = auth or VkAuth(settings)
    vk_client = VkClient(settings, token_provider=auth)
    collectors = {"demo": DemoCollector(), "vk": VkCollector(settings, vk_client)}
    if collector is not None:
        collectors[settings.data_mode] = collector
    services = {
        mode: SearchService(
            settings.model_copy(update={"data_mode": mode}), sessions, item, cache,
            credential_partition=auth.cache_partition if mode == "vk" else None,
        )
        for mode, item in collectors.items()
    }

    @asynccontextmanager
    async def lifespan(app):
        init_database(engine)
        yield
        engine.dispose()
        if hasattr(cache, "close"):
            cache.close()
        for item in collectors.values():
            if hasattr(item, "close"):
                item.close()
        vk_client.close()
        auth.close()

    app = FastAPI(title="Аудитория VK — учебный проект", version="0.2.0", lifespan=lifespan)
    # Сервис локальный; произвольные Host не должны открывать настройки через DNS rebinding.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "backend", "testserver"])
    app.state.service = services[settings.data_mode]
    app.state.services = services
    app.state.auth = auth
    app.state.engine = engine
    app.include_router(auth.router)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request, _error):
        return JSONResponse(status_code=503, content={"detail": "База данных недоступна. Проверьте контейнер postgres."})

    @app.exception_handler(AuthError)
    async def auth_error(_request, error):
        return JSONResponse(status_code=error.status_code, content={"detail": str(error)},
                            headers={"Cache-Control": "no-store"})

    @app.get("/health")
    def health():
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "mode": settings.data_mode, "redis": "ok" if cache.healthy() else "unavailable"}

    @app.get("/api/suggestions")
    def suggestions(q: str = Query("", max_length=200), data_mode: Literal["demo", "vk"] | None = None):
        return {"suggestions": services[data_mode or settings.data_mode].suggestions(q)}

    @app.post("/api/search")
    def search(request: SearchRequest):
        mode = request.data_mode or settings.data_mode
        locked = False
        try:
            if mode == "vk":
                locked = auth.connection_lock.acquire(blocking=False)
                if not locked:
                    raise CollectionBusy("Уже выполняется операция VK. Дождитесь её завершения.")
                if not auth.status()["connected"]:
                    raise HTTPException(status_code=401, detail="Войдите через VK ID в разделе подключения VK.")
            return services[mode].search(request.query, request.modes)
        except CollectionBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except CollectionError as error:
            raise HTTPException(status_code=502, detail=str(error)) from None
        finally:
            if locked:
                auth.connection_lock.release()

    @app.post("/api/vk/check")
    def check_vk(request: Request):
        # Streamlit обращается с сервера. Чужой сайт не может отправить этот заголовок без CORS.
        origin = request.headers.get("origin")
        if request.headers.get("x-requested-with") != "AudienceDashboard" or (
            origin and origin not in {settings.vk_redirect_uri, settings.frontend_public_url}
        ):
            raise HTTPException(status_code=403, detail="Откройте проверку из интерфейса проекта.")
        if not auth.connection_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Дождитесь завершения текущей операции VK.")
        try:
            vk_client.call("groups.search", deadline=time.monotonic() + 45, q="велосипеды", count=1)
        except VkApiError as error:
            raise HTTPException(status_code=502, detail=str(error)) from None
        finally:
            auth.connection_lock.release()
        return {"ok": True, "detail": "Поиск сообществ доступен. Теперь можно проверить небольшой сбор аудитории."}

    return app
