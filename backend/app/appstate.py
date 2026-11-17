"""app_state key/value helpers (JSONB values)."""
from typing import Any, Optional

from sqlalchemy.dialects.postgresql import insert

from .db import session_scope
from .models import AppState, utcnow


def set_state(key: str, value: Any) -> None:
    stmt = insert(AppState).values(key=key, value=value, updated_at=utcnow())
    with session_scope() as s:
        s.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at}))


def get_state(key: str) -> Optional[Any]:
    with session_scope() as s:
        row = s.get(AppState, key)
        return row.value if row else None
