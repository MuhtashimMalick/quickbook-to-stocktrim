"""Company file named in the Web Connector's authenticate answer. Offline: no database, no network."""
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from pydantic import ValidationError
from qbwc_kit import soap

from app.config import Settings
from app.qbwc.service import LoggingQBWCService, _result_texts

PATH = r"C:\QB Files\specialty archery, llc. – live.QBW"  # spaces, comma, period, backslashes, EN DASH
ROOT = Path(__file__).resolve().parents[2]


class Auth:
    def __init__(self, tasks: list):
        self.tasks = tasks

    def authenticate(self, username: str, password: str) -> bool:
        return (username, password) == ("u", "p")

    def tasks_for(self, username: str) -> list:
        return self.tasks


def auth(service: LoggingQBWCService, password: str = "p") -> str:
    return service.dispatch(soap.build_request("authenticate", [("strUserName", "u"), ("strPassword", password)]))


def svc(tasks: list, company_file: str = "", monkeypatch=None) -> LoggingQBWCService:
    s = LoggingQBWCService(Auth(tasks), company_file=company_file)
    s._touch = lambda key: None  # no database here
    return s


def slots(envelope: str) -> list[str]:
    return _result_texts(envelope, "authenticate")


def test_work_queued_names_the_file():
    out = slots(auth(svc([object()], PATH)))
    assert out[0] and out[1] == PATH


def test_no_setting_keeps_the_current_answer():
    out = slots(auth(svc([object()])))
    assert out[0] and out[1] == ""


def test_idle_and_refused_are_untouched():
    assert slots(auth(svc([], PATH))) == ["", "none"]
    assert slots(auth(svc([object()], PATH), password="bad")) == ["", "nvu"]


def test_path_round_trips_as_utf8_and_special_characters_are_escaped():
    for path in (PATH, r"C:\A&B <x> – c.QBW"):
        raw = auth(svc([object()], path)).encode("utf-8")
        assert path.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").encode("utf-8") in raw and b"<x>" not in raw  # escaped, raw bytes are UTF-8
        assert 'encoding="utf-8"'.encode() in raw and "–".encode("utf-8") in raw
        assert slots(raw.decode("utf-8"))[1] == path
        ET.fromstring(raw)  # well-formed


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, database_url="postgresql+psycopg://u:p@h/d", qbwc_username="u", qbwc_password="p", admin_token="a-real-token", jwt_secret="j" * 40,
                    qwc_owner_id=uuid.uuid4(), qwc_file_id=uuid.uuid4(), **kw)


def test_config_validation():
    assert _settings().qbwc_company_file == ""
    assert _settings(qbwc_company_file=f"  {PATH}\t ").qbwc_company_file == PATH
    for bad in ("C:\a\nb.QBW", "C:\a\x00.QBW", "C:\\" + "x" * 300):
        with pytest.raises(ValidationError):
            _settings(qbwc_company_file=bad)


def test_compose_and_env_example_carry_the_setting():
    assert "QBWC_COMPANY_FILE: ${QBWC_COMPANY_FILE:-}" in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert f"# QBWC_COMPANY_FILE='{PATH}'" in (ROOT / ".env.example").read_text(encoding="utf-8")
