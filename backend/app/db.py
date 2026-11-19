from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


# ponytail: module-level engine (one DB per process); inject if multi-tenant ever matters.
_sessionmaker: "sessionmaker[Session] | None" = None


def init_engine(url: str) -> None:
    global _sessionmaker
    if _sessionmaker is not None:
        _sessionmaker.kw["bind"].dispose()
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)  # failed statements must not echo bound values (hashes, tokens)
    _sessionmaker = sessionmaker(engine, expire_on_commit=False)


def get_engine() -> Engine:
    assert _sessionmaker is not None, "init_engine() not called"
    return _sessionmaker.kw["bind"]


@contextmanager
def session_scope() -> Iterator[Session]:
    assert _sessionmaker is not None, "init_engine() not called"
    with _sessionmaker() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
