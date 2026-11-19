import os
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from qbwc_kit.qbxml.types import TRANSACTION_ENTITIES
from qbwc_kit.testing import FakeQuickBooks, FakeWebConnector, UpdateResult, service_transport
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError

from alembic import command
from app.config import DatabaseSettings, Settings
from app.db import Base
from app.main import build_app

os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-" + "x" * 24)  # Settings requires it; not a real secret
USER, PASSWORD, TOKEN = "qb", "s3cr3t-pw-xyz", "admin-tok"
HEADERS = {"X-Admin-Token": TOKEN}
ADMIN_ROUTES = [("get", "/api/v1/qwc"), ("post", "/api/v1/jobs/customers"), ("post", "/api/v1/jobs/all-reads"), ("get", "/api/v1/jobs"),
                ("get", "/api/v1/jobs/1"), ("get", "/api/v1/records/customers"), ("get", "/api/v1/log"),
                ("get", "/api/v1/health"), ("post", "/api/v1/scheduler/run-now")]


def _default_test_db_url() -> str:
    """TEST_DATABASE_URL, else built from POSTGRES_USER/PASSWORD/DB (the compose db), else DATABASE_URL; database name + `_test`."""
    if url := os.environ.get("TEST_DATABASE_URL"):
        return url
    if (user := os.environ.get("POSTGRES_USER")) and (pw := os.environ.get("POSTGRES_PASSWORD")):
        base = URL.create("postgresql+psycopg", user, pw, "127.0.0.1", 5432, os.environ.get("POSTGRES_DB", "qb_stocktrim"))
    else:
        base = make_url(DatabaseSettings().database_url)
    return base.set(database=f"{base.database}_test").render_as_string(hide_password=False)


TEST_DB_URL = _default_test_db_url()
BACKEND_DIR = Path(__file__).resolve().parents[1]


def alembic_cfg(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["url"] = url
    return cfg


def admin_engine(url: str):
    """Autocommit engine on the maintenance database, for CREATE/DROP DATABASE."""
    return create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5})


def create_database(url: str, name: str, recreate: bool = False) -> None:
    with admin_engine(url).connect() as c:
        if recreate:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        if not c.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}):
            c.execute(text(f'CREATE DATABASE "{name}"'))


@pytest.fixture(scope="session")
def db_url() -> str:
    name = make_url(TEST_DB_URL).database or ""
    if not name.endswith("_test"):  # clean_db TRUNCATEs: never point it at a real database
        pytest.exit(f"refusing to run tests on database {name!r}: its name must end with '_test'", returncode=1)
    try:
        create_database(TEST_DB_URL, name, recreate=True)  # fresh DB each session = always the current 0001
    except OperationalError as exc:
        pytest.exit(f"PostgreSQL is not reachable at {make_url(TEST_DB_URL).render_as_string()}: start it with "
                    f"`docker compose up -d db` (from the repo root, with a .env copied from .env.example). {exc}", returncode=1)
    command.upgrade(alembic_cfg(TEST_DB_URL), "head")
    return TEST_DB_URL


@pytest.fixture
def clean_db(db_url: str) -> str:
    engine = create_engine(db_url)
    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    engine.dispose()
    return db_url


class QB(FakeQuickBooks):
    """FakeQuickBooks plus (L4) TxnID on transaction Adds and a RefNumber filter on queries."""

    def _add(self, response_name, attrs, entity, node):
        before = len(self.entities.get(entity, []))
        out = super()._add(response_name, attrs, entity, node)
        if entity in TRANSACTION_ENTITIES:
            rec = self.entities[entity][before]
            rec.pop("ListID", None)
            rec["TxnID"] = f"TXN-{entity}-{before + 1}"
            from qbwc_kit.testing import _render_record  # noqa: PLC0415
            body = _render_record(entity, rec)
            return f"<{response_name}{attrs} statusCode=\"0\" statusSeverity=\"Info\" statusMessage=\"Status OK\">{body}</{response_name}>"
        return out

    def _query(self, response_name, attrs, entity, node):
        ref = node.find("RefNumber")
        if ref is None:
            return super()._query(response_name, attrs, entity, node)
        original = self.entities
        self.entities = {**original, entity: [r for r in original.get(entity, []) if r.get("RefNumber") == ref.text]}
        try:
            return super()._query(response_name, attrs, entity, node)
        finally:
            self.entities = original


def make_records(entity: str, n: int) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    for i in range(n):
        rec: dict[str, Any] = {"EditSequence": "1", "TimeModified": "2024-01-01T00:00:00", "Name": f"{entity} {i}"}
        if entity in TRANSACTION_ENTITIES:
            rec.update(TxnID=f"{entity}-{i}", RefNumber=f"R{i}")
        else:
            rec["ListID"] = f"{entity}-{i}"
        recs.append(rec)
    return recs


def rs_error(name: str, code: int) -> str:
    return (f'<?xml version="1.0"?><QBXML><QBXMLMsgsRs><{name} statusCode="{code}" statusSeverity="Error" '
            f'statusMessage="boom {code}"/></QBXMLMsgsRs></QBXML>')


class Env:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.app = build_app(settings)
        self.client = TestClient(self.app, client=("127.0.0.1", 50000))

    def queue(self, job_type: str, body: Optional[dict] = None):
        return self.client.post(f"/api/v1/jobs/{job_type}", json=body, headers=HEADERS)

    def get(self, path: str):
        return self.client.get(path, headers=HEADERS)

    def job(self, job_id: int) -> dict:
        return self.get(f"/api/v1/jobs/{job_id}").json()

    def connector(self, version=(13, 0), http=False, password=PASSWORD) -> FakeWebConnector:
        if http:
            def transport(body: str) -> str:
                return self.client.post("/qbwc", content=body, headers={"Content-Type": "text/xml"}).text
        else:
            transport = service_transport(self.app.state.qbwc_service)
        return FakeWebConnector(transport, USER, password, qbxml_version=version)

    def run(self, responder: Callable[[str], str], version=(13, 0), http=False) -> UpdateResult:
        return self.connector(version, http).run_update(responder)

    def records(self, entity: str) -> list[dict]:
        return self.get(f"/api/v1/records/{entity}?limit=500").json()


@pytest.fixture
def make_env(tmp_path, clean_db):
    def _make(writes: bool = False, log_dir=None) -> Env:
        return Env(Settings(
            _env_file=None, qbwc_username=USER, qbwc_password=PASSWORD, admin_token=TOKEN,
            qwc_owner_id=uuid.uuid4(), qwc_file_id=uuid.uuid4(),
            database_url=clean_db, fixtures_dir=tmp_path / "fx",
            writes_enabled=writes, run_every_n_seconds=120, log_dir=log_dir or tmp_path / 'logs',
        ))
    return _make


@pytest.fixture
def env(make_env) -> Env:
    return make_env()


@pytest.fixture(autouse=True)
def _fresh_shared_limiters():
    from app import auth  # noqa: PLC0415
    from app.stocktrim.limiter import reset_shared_limiters  # noqa: PLC0415
    from app.sync import engine  # noqa: PLC0415

    auth._hits.clear()
    engine._SHUTTING_DOWN.clear()  # a lifespan shutdown in an earlier test must not block this test's real runs
    reset_shared_limiters()
