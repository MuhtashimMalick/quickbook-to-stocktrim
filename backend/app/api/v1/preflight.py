import threading
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from ...preflight.report import PreflightOptions, build_report
from ...preflight.schema import PreflightReport

router = APIRouter()
_RUNNING = threading.Lock()  # ponytail: one run per process; two runs would double the StockTrim request rate


def get_stocktrim_client_kw() -> dict[str, Any]:
    """Extra StockTrimClient arguments; tests override this dependency (transport, sleep, max_attempts)."""
    return {}


@router.post("/preflight", response_model=PreflightReport, response_model_exclude_none=True)
def preflight(request: Request, body: Optional[PreflightOptions] = None, client_kw: dict[str, Any] = Depends(get_stocktrim_client_kw)) -> PreflightReport:
    """Read-only dry run (QuickBooks snapshot + StockTrim GETs). Seconds by default; nothing is written anywhere. 409 while another run is in progress."""
    if not _RUNNING.acquire(blocking=False):
        raise HTTPException(409, "preflight_running")
    try:
        return build_report(request.app.state.settings, body or PreflightOptions(), client_kw)
    finally:
        _RUNNING.release()
