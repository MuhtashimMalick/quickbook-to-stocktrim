import logging
from typing import Optional

from fastapi import FastAPI
from qbwc_kit.server import create_app

from .api import router
from .auth import DbAuthenticator, end_session
from .config import Settings, get_settings
from .db import create_all, init_engine
from .qbwc_service import LoggingQBWCService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def build_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    init_engine(settings.database_url)
    create_all()
    app = FastAPI(title="QuickBooks <-> StockTrim POC (QuickBooks side)")
    app.state.settings = settings
    service = LoggingQBWCService(DbAuthenticator(settings), on_session_end=end_session)
    app.state.qbwc_service = service
    create_app(service, endpoint_url=settings.app_url, path="/qbwc", app=app)
    app.include_router(router)
    return app


def __getattr__(name: str) -> FastAPI:
    # Lazy `app` so importing this module (tests) doesn't require a .env; uvicorn does getattr(module, "app").
    if name == "app":
        return build_app()
    raise AttributeError(name)
