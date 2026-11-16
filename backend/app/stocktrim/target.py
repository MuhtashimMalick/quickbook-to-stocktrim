"""Non-secret id of the StockTrim account a push targets; scopes the BOM ledger and push runs so test and production never mix."""
import hashlib
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ..config import Settings


def stocktrim_target(s: "Settings") -> str:
    """f'{environment}|{normalised base URL}|{sha256(auth_id)[:12]}' (the hash prefix is not reversible to the id)."""
    auth = s.stocktrim_auth_id.get_secret_value() if s.stocktrim_auth_id else ""
    return f"{s.stocktrim_environment}|{str(httpx.URL(s.stocktrim_base_url)).rstrip('/')}|{hashlib.sha256(auth.encode()).hexdigest()[:12]}"
