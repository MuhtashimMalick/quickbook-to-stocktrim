"""Protocol plumbing only: log every QBWC callback and stash the raw response XML for tasks.

Works around qbwc-kit 0.1.3 (which we do not patch): it hands tasks parsed responses only,
and logs just some callbacks.
"""
import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from xml.etree import ElementTree as ET

from qbwc_kit import QBWCService, soap
from qbwc_kit.session import UnknownTicket

logger = logging.getLogger("qb_poc.qbwc")
RAW_RESPONSE_KEY = "raw_response_xml"
_ROOT_RQ = re.compile(r"<QBXMLMsgsRq[^>]*>\s*<(\w+)")


def _result_texts(envelope: str, method: str) -> list[str]:
    for el in ET.fromstring(envelope).iter(f"{{{soap.QBWC_NS}}}{method}Result"):
        return [s.text or "" for s in el] or [el.text or ""]
    return ["<soap fault>"]


def _describe(call: soap.SoapCall) -> dict:
    get = call.get
    if call.method == "authenticate":
        return {"username": get("strUserName")}  # never the password
    if call.method == "clientVersion":
        return {"version": call.get("strVersion") or call.positional(0)}
    if call.method == "serverVersion":
        return {}
    f: dict = {"ticket": get("ticket")[:8]}
    if call.method == "sendRequestXML":
        f.update(company=get("strCompanyFileName"), qbXML=f"{get('qbXMLMajorVers')}.{get('qbXMLMinorVers')}")
    elif call.method in ("receiveResponseXML", "connectionError"):
        f.update(hresult=get("hresult"), message=get("message"), response_len=len(get("response")))
    return f


def _summary(method: str, envelope: str) -> dict:
    texts = _result_texts(envelope, method)
    if method == "authenticate":  # status slot only, never the ticket
        return {"status": texts[-1]}
    if method == "sendRequestXML":
        m = _ROOT_RQ.search(texts[0])
        return {"request_len": len(texts[0]), "root": m.group(1) if m else "-"}
    return {"result": " ".join(texts)[:200] or "status=''"}


_RAW = {"ticket", "qbXML"}  # printed unquoted on the console


def _emit(arrow: str, method: str, fields: dict) -> None:
    """The single log call: console text and the structured JSONL record come from the same fields."""
    text = " ".join(str(v) if k == "result" else f"{k}={v if k in _RAW else repr(v)}" for k, v in fields.items())
    logger.info("QBWC %s %s %s", arrow, method, text, extra={"qbwc": {"direction": "in" if arrow == "->" else "out", "event": method, **fields}})


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")
        return json.dumps({"ts": ts, **record.qbwc}, default=str)


class _DirHandler(TimedRotatingFileHandler):
    def _open(self):  # create the log dir on first write
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        return super()._open()


def setup_file_log(log_dir: Path, retention_days: int) -> None:
    """Attach the daily JSONL handler to the qbwc logger; replaces (never stacks) any earlier one."""
    for h in [h for h in logger.handlers if isinstance(h, _DirHandler)]:
        logger.removeHandler(h)
        h.close()
    h = _DirHandler(Path(log_dir) / "qbwc.jsonl", when="midnight", backupCount=retention_days, encoding="utf-8", delay=True, utc=True)
    h.setFormatter(JsonLineFormatter())
    h.addFilter(lambda r: hasattr(r, "qbwc"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


class LoggingQBWCService(QBWCService):
    def dispatch(self, body: "str | bytes") -> str:
        try:
            call = soap.parse_request(body)
        except soap.SoapError:
            return super().dispatch(body)
        _emit("->", call.method, _describe(call))
        if call.method == "receiveResponseXML":
            try:
                self.store.get(call.get("ticket")).context.state[RAW_RESPONSE_KEY] = call.get("response")
            except UnknownTicket:
                pass
        result = super().dispatch(body)
        _emit("<-", call.method, _summary(call.method, result))
        return result
