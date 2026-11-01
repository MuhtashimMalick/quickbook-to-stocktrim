import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from qbwc_kit.testing import FakeWebConnector

from app.config import Settings

from .conftest import HEADERS, PASSWORD, QB, TOKEN, USER, Env, make_records

PATHS = [("get", "/qwc"), ("post", "/jobs/customers"), ("post", "/jobs/all-reads"), ("get", "/jobs"),
         ("get", "/jobs/1"), ("get", "/records/customers"), ("get", "/log"), ("get", "/docs"),
         ("get", "/openapi.json"), ("get", "/redoc"), ("get", "/nope")]
PROXY = [{"X-Forwarded-For": "203.0.113.5"}, {"X-Forwarded-Host": "x.ngrok-free.dev"}, {"Forwarded": "for=203.0.113.5"},
         {"X-Forwarded-Proto": "https"}]


@pytest.mark.parametrize("method,path", PATHS)
def test_proxied_requests_are_404(env, method, path):
    for extra in PROXY:
        r = getattr(env.client, method)(path, headers={**HEADERS, **extra})
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    remote = TestClient(env.app, client=("203.0.113.5", 1))
    assert getattr(remote, method)(path, headers=HEADERS).status_code == 404


def test_local_still_works(env):
    assert env.get("/jobs").status_code == 200


def test_qbwc_passes_gate_with_proxy_headers(env):
    xff = {"X-Forwarded-For": "203.0.113.5"}
    remote = TestClient(env.app, client=("203.0.113.5", 1))
    assert "definitions" in remote.get("/qbwc?wsdl", headers=xff).text
    env.queue("customers")
    c = FakeWebConnector(lambda body: remote.post("/qbwc", content=body, headers={"Content-Type": "text/xml", **xff}).text,
                USER, PASSWORD, qbxml_version=(13, 0))
    res = c.run_update(QB(entities={"Customer": make_records("Customer", 2)}))
    assert res.authenticated and len(env.records("customers")) == 2


def test_qwc_and_wsdl_for_ngrok_url(tmp_path):
    url = "https://unrupturable-unreticently-carl.ngrok-free.dev/qbwc"
    s = Settings(_env_file=None, qbwc_username="specialty-archery", qbwc_password=PASSWORD, admin_token=TOKEN,
                 qwc_owner_id=uuid.UUID("977e21e5-98f1-440d-ac64-461878a8262c"),
                 qwc_file_id=uuid.UUID("21dfbdc4-f9b7-44bc-aed1-605d1aa21cc0"), app_url=url,
                 database_url=f"sqlite:///{tmp_path / 't.db'}", fixtures_dir=tmp_path / "fx")
    e = Env(s)
    r = e.get("/qwc")
    assert f"<AppURL>{url}</AppURL>" in r.text
    assert "<UserName>specialty-archery</UserName>" in r.text
    assert "<AppName>Specialty Archery StockTrim</AppName>" in r.text
    assert "<OwnerID>{977E21E5-98F1-440D-AC64-461878A8262C}</OwnerID>" in r.text
    assert "<FileID>{21DFBDC4-F9B7-44BC-AED1-605D1AA21CC0}</FileID>" in r.text
    assert "<RunEveryNSeconds>600</RunEveryNSeconds>" in r.text
    assert url in e.client.get("/qbwc?wsdl").text


@pytest.mark.parametrize("bad", ["http://example.com/qbwc", "https://x.dev/other", "ftp://localhost/qbwc"])
def test_app_url_validator_rejects(bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, qbwc_username="u", qbwc_password="p", admin_token="t",
                 qwc_owner_id=uuid.uuid4(), qwc_file_id=uuid.uuid4(), app_url=bad)


def test_qwc_filename_validated():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, qbwc_username="u", qbwc_password="p", admin_token="t",
                 qwc_owner_id=uuid.uuid4(), qwc_file_id=uuid.uuid4(), qwc_filename='a"b.qwc')


def test_client_version_logged(env, caplog):
    caplog.set_level(logging.INFO)
    env.queue("customers")
    c = env.connector(http=True)
    res = c.run_update(QB())
    assert f"QBWC -> clientVersion version={c.client_version!r}" in caplog.text
    assert PASSWORD not in caplog.text
    assert res.ticket and res.ticket not in caplog.text
