from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.annotation import page_router as annotation_page_router
from app.api.annotation import router as annotation_router
from app.api.auth import router as auth_router
from app.api.automations import router as automations_router
from app.api.connectors import callback_router as connector_callback_router
from app.api.connectors import router as connectors_router
from app.api.conversations import router as conversations_router
from app.api.cost import router as cost_router
from app.api.cowork import router as cowork_router
from app.api.dependencies import enforce_ip_rate_limit
from app.api.editor import router as editor_router
from app.api.health import router as health_router
from app.api.integrations import router as integrations_router
from app.api.library import router as library_router
from app.api.memory import router as memory_router
from app.api.providers import router as providers_router
from app.api.retrieval import router as retrieval_router
from app.api.runs import router as runs_router
from app.api.sources import router as sources_router
from app.core.config import Settings, get_settings
from app.core.db import close_database
from app.core.desktop_security import DesktopLaunchTokenMiddleware
from app.core.logging import configure_logging
from app.core.queue import close_run_queue
from app.core.redis import close_redis
from app.core.trace import TraceIdMiddleware


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    yield
    await close_run_queue()
    await close_redis()
    await close_database()


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = get_settings() if settings is None else settings
    app = FastAPI(title="WorkPilot API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TraceIdMiddleware)
    app.add_middleware(
        DesktopLaunchTokenMiddleware,
        enabled=runtime_settings.desktop_mode_enabled,
        launch_token=runtime_settings.desktop_launch_token.get_secret_value(),
    )
    if runtime_settings.desktop_mode_enabled:
        # Tauri 2 在不同平台使用 tauri:// 或 *.tauri.localhost 资产 origin。
        # 仅桌面模式开放这三个精确 origin；实际 API 请求仍必须通过
        # DesktopLaunchTokenMiddleware，预检 OPTIONS 由外层 CORS 中间件就地回应。
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "tauri://localhost",
                "http://tauri.localhost",
                "https://tauri.localhost",
                # `tauri dev` 使用 Next.js devUrl；请求仍必须携带每次启动随机
                # token，因此只开放这两个精确本机开发 origin 不会扩大文件权限。
                "http://127.0.0.1:3000",
                "http://localhost:3000",
            ],
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["content-type", "x-workpilot-launch-token"],
            expose_headers=["content-type"],
        )
    rate_limited = [Depends(enforce_ip_rate_limit)]
    app.include_router(annotation_page_router, dependencies=rate_limited)
    app.include_router(annotation_router, dependencies=rate_limited)
    app.include_router(auth_router, dependencies=rate_limited)
    app.include_router(automations_router, dependencies=rate_limited)
    app.include_router(cost_router, dependencies=rate_limited)
    app.include_router(conversations_router, dependencies=rate_limited)
    app.include_router(connectors_router, dependencies=rate_limited)
    app.include_router(connector_callback_router, dependencies=rate_limited)
    app.include_router(cowork_router, dependencies=rate_limited)
    app.include_router(editor_router, dependencies=rate_limited)
    app.include_router(health_router)
    app.include_router(integrations_router, dependencies=rate_limited)
    app.include_router(library_router, dependencies=rate_limited)
    app.include_router(memory_router, dependencies=rate_limited)
    app.include_router(providers_router, dependencies=rate_limited)
    app.include_router(retrieval_router, dependencies=rate_limited)
    app.include_router(runs_router, dependencies=rate_limited)
    app.include_router(sources_router, dependencies=rate_limited)
    return app


app = create_app()
