import uuid
from typing import Any, Callable, Optional

import pytest
from fastapi.testclient import TestClient
from qbwc_kit.testing import FakeQuickBooks, FakeWebConnector, UpdateResult, service_transport
from qbwc_kit.qbxml.types import TRANSACTION_ENTITIES

from app.config import Settings
from app.main import build_app

USER, PASSWORD, TOKEN = "qb", "s3cr3t-pw-xyz", "admin-tok"
HEADERS = {"X-Admin-Token": TOKEN}


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
        return self.client.post(f"/jobs/{job_type}", json=body, headers=HEADERS)

    def get(self, path: str):
        return self.client.get(path, headers=HEADERS)

    def job(self, job_id: int) -> dict:
        return self.get(f"/jobs/{job_id}").json()

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
        return self.get(f"/records/{entity}?limit=500").json()


@pytest.fixture
def make_env(tmp_path):
    def _make(writes: bool = False, log_dir=None) -> Env:
        return Env(Settings(
            _env_file=None, qbwc_username=USER, qbwc_password=PASSWORD, admin_token=TOKEN,
            qwc_owner_id=uuid.uuid4(), qwc_file_id=uuid.uuid4(),
            database_url=f"sqlite:///{tmp_path / 'test.db'}", fixtures_dir=tmp_path / "fx",
            writes_enabled=writes, run_every_n_seconds=120, log_dir=log_dir or tmp_path / 'logs',
        ))
    return _make


@pytest.fixture
def env(make_env) -> Env:
    return make_env()
