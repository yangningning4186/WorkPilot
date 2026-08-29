from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.auth import router as auth_router
from app.api.automations import router as automations_router
from app.api.connectors import callback_router as connector_callback_router
from app.api.connectors import router as connectors_router
from app.api.conversations import router as conversations_router
from app.api.cost import router as cost_router
from app.api.cowork import router as cowork_router
from app.api.health import router as health_router
from app.api.integrations import router as integrations_router
from app.api.memory import router as memory_router
from app.api.messaging import router as messaging_router
from app.api.personas import router as personas_router
from app.api.providers import router as providers_router
from app.api.runs import router as runs_router
from app.core.config import Settings, get_settings
from app.core.db import close_database
from app.core.desktop_security import DesktopLaunchTokenMiddleware
from app.core.logging import configure_logging
from app.core.queue import close_run_queue
from app.core.trace import TraceIdMiddleware
from app.cowork_store.factory import close_local_cowork_stores, initialize_local_cowork_stores
from app.telemetry import initialize_telemetry_store
from app.worker.local_runtime import EmbeddedWorkerRuntime


async def _safe_request_validation_error(
    _request: Request,
    _error: Exception,
) -> JSONResponse:
    """Never reflect rejected request values from secret-bearing control-plane forms.

    FastAPI's default 422 body includes Pydantic's ``input`` field.  Provider, Connector and
    MCP payloads can contain credentials, and a malformed sibling field must not echo those
    credentials back into the response, browser logs or telemetry.
    """

    return JSONResponse(status_code=422, content={"detail": "请求参数无效"})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    embedded_worker: EmbeddedWorkerRuntime | None = None
    await initialize_local_cowork_stores(settings)
    # worker 的费用闸门与 Skill/记忆后处理会立刻访问 telemetry.db。必须先建表再启动
    # consumer，否则旧库/空库上的后台作业会以 no such table: llm_calls 进入重试风暴。
    await initialize_telemetry_store()
    embedded_worker = await EmbeddedWorkerRuntime.start(settings)
    app.state.embedded_worker = embedded_worker
    yield
    if embedded_worker is not None:
        await embedded_worker.stop()
    await close_local_cowork_stores()
    await close_run_queue()
    await close_database()


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = get_settings() if settings is None else settings
    app = FastAPI(title="WorkPilot API", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(RequestValidationError, _safe_request_validation_error)
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
    app.include_router(auth_router)
    app.include_router(automations_router)
    app.include_router(cost_router)
    app.include_router(conversations_router)
    app.include_router(connectors_router)
    app.include_router(connector_callback_router)
    app.include_router(personas_router)
    app.include_router(cowork_router)
    app.include_router(health_router)
    app.include_router(integrations_router)
    app.include_router(memory_router)
    app.include_router(messaging_router)
    app.include_router(providers_router)
    app.include_router(runs_router)
    return app


app = create_app()
