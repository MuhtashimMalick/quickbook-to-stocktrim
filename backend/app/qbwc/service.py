"""Protocol plumbing only: log every QBWC callback and stash the raw response XML for tasks.

Works around qbwc-kit 0.1.3 (which we do not patch): it hands tasks parsed responses only,
and logs just some callbacks.
"""
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from xml.etree import ElementTree as ET

from qbwc_kit import QBWCService, soap
from qbwc_kit.session import UnknownTicket

from ..appstate import set_state
from ..models import utcnow
from .textrepair import repair_qbxml

logger = logging.getLogger(__name__)
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


@dataclass
class LoggingQBWCService(QBWCService):
    company_file: str = ""  # QBWC_COMPANY_FILE: named in the authenticate answer when work is queued ("" = whichever file is open)

    def _do_authenticate(self, call: soap.SoapCall) -> list[str]:
        out = list(super()._do_authenticate(call))
        if self.company_file and out[0] and out[1] == "":  # authenticated with work queued (idle = "none", refused = "nvu": untouched)
            out[1] = self.company_file
            logger.info("QBWC company file named in authenticate: configured=True len=%d", len(self.company_file))
        return out

    def _touch(self, key: str) -> None:
        last = self.__dict__.setdefault("_contact_at", {})  # per state key: monotonic time of the last write (throttled to once a minute)
        if time.monotonic() - last.get(key, -1e9) < 60:
            return
        last[key] = time.monotonic()
        try:
            set_state(key, {"at": utcnow().isoformat()})
        except Exception as e:  # noqa: BLE001 - health bookkeeping must never break the Web Connector
            logger.warning("%s not recorded: %s", key, type(e).__name__)

    def dispatch(self, body: "str | bytes") -> str:
        try:
            call = soap.parse_request(body)
        except soap.SoapError:
            logger.warning("unparseable SOAP request: %d chars, receiveResponseXML=%s", len(body), "receiveResponseXML" in str(body))
            return super().dispatch(body)
        _emit("->", call.method, _describe(call))
        if call.method == "receiveResponseXML":
            raw = call.get("response")  # real QBWC always sends named parameters (qbwc-kit also falls back to positional(1))
            try:
                self.store.get(call.get("ticket")).context.state[RAW_RESPONSE_KEY] = raw  # fixtures stay raw
            except UnknownTicket:
                pass
            # Wire format: the qbXML is XML-escaped (or CDATA) text inside the envelope; parse_request has
            # already decoded it to the plain qbXML string. Rebuild the envelope with the repaired string
            # (build_request re-escapes it) so qbwc-kit parses repaired text.
            if (fixed := repair_qbxml(raw)) != raw:
                body = soap.build_request(call.method, [(k, fixed if k == "response" else v) for k, v in call.params.items()])
        result = super().dispatch(body)
        if call.method == "authenticate":
            self._touch("last_qbwc_auth_refused" if _result_texts(result, "authenticate")[-1] == "nvu" else "last_qbwc_contact")  # [ticket, status]: "nvu" = credentials rejected; "none" (idle) is a good login
        _emit("<-", call.method, _summary(call.method, result))
        return result
