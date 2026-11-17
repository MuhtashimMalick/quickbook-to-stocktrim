import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from qbwc_kit.server import create_app

from . import jobs
from .api.v1.router import router
from .config import Settings, get_settings
from .db import init_engine
from .logging_config import setup_logging
from .qbwc.auth import DbAuthenticator, end_session
from .qbwc.gate import QBWC_PATH, TunnelGate
from .qbwc.service import LoggingQBWCService, setup_file_log
from .quickbooks.tasks import WRITE_JOB_TYPES
from .sync.engine import recover_push_runs, shutdown

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    jobs.recover_interrupted(WRITE_JOB_TYPES)
    recover_push_runs()
    yield
    await asyncio.to_thread(shutdown)  # cancel the active push run (resumable) and wait for its worker


def build_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    init_engine(settings.database_url)
    setup_file_log(settings.log_dir, settings.log_retention_days)
    app = FastAPI(lifespan=lifespan, title="QuickBooks <-> StockTrim POC (QuickBooks side)")
    app.state.settings = settings
    service = LoggingQBWCService(DbAuthenticator(settings), on_session_end=end_session)
    app.state.qbwc_service = service
    create_app(service, endpoint_url=settings.app_url, path=QBWC_PATH, app=app)
    app.add_middleware(TunnelGate)
    app.include_router(router, prefix="/api/v1")
    return app


def __getattr__(name: str) -> FastAPI:
    # Lazy `app` so importing this module (tests) doesn't require a .env; uvicorn does getattr(module, "app").
    if name == "app":
        return build_app()
    raise AttributeError(name)
