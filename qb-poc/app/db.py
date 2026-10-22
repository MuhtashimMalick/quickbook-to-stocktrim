from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


# ponytail: module-level engine (one DB per process); inject if multi-tenant ever matters.
_sessionmaker: "sessionmaker[Session] | None" = None


def init_engine(url: str) -> None:
    global _sessionmaker
    args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=args, pool_pre_ping=True)
    _sessionmaker = sessionmaker(engine, expire_on_commit=False)


def create_all() -> None:
    from . import models  # noqa: F401  (register tables)

    assert _sessionmaker is not None
    Base.metadata.create_all(_sessionmaker.kw["bind"])


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
