"""Protocol plumbing only: log every QBWC callback and stash the raw response XML for tasks.

Works around qbwc-kit 0.1.3 (which we do not patch): it hands tasks parsed responses only,
and logs just some callbacks.
"""
import logging
import re
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


def _describe(call: soap.SoapCall) -> str:
    get = call.get
    if call.method == "authenticate":
        return f"username={get('strUserName')!r}"  # never the password
    ticket = get("ticket")[:8]
    if call.method == "sendRequestXML":
        return f"ticket={ticket} company={get('strCompanyFileName')!r} qbXML={get('qbXMLMajorVers')}.{get('qbXMLMinorVers')}"
    if call.method in ("receiveResponseXML", "connectionError"):
        return f"ticket={ticket} hresult={get('hresult')!r} message={get('message')!r} response_len={len(get('response'))}"
    return f"ticket={ticket}"


def _summary(method: str, envelope: str) -> str:
    texts = _result_texts(envelope, method)
    if method == "authenticate":  # status slot only, never the ticket
        return f"status={texts[-1]!r}"
    if method == "sendRequestXML":
        m = _ROOT_RQ.search(texts[0])
        return f"request_len={len(texts[0])} root={m.group(1) if m else '-'}"
    return " ".join(texts)[:200]


class LoggingQBWCService(QBWCService):
    def dispatch(self, body: "str | bytes") -> str:
        try:
            call = soap.parse_request(body)
        except soap.SoapError:
            return super().dispatch(body)
        logger.info("QBWC -> %s %s", call.method, _describe(call))
        if call.method == "receiveResponseXML":
            try:
                self.store.get(call.get("ticket")).context.state[RAW_RESPONSE_KEY] = call.get("response")
            except UnknownTicket:
                pass
        result = super().dispatch(body)
        logger.info("QBWC <- %s %s", call.method, _summary(call.method, result))
        return result
