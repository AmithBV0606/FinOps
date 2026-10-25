from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from finops.config import get_settings

from .models import Base

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def configure(url: str | None = None) -> Engine:
    """(Re)bind the module to a database URL and create tables if missing."""
    global _engine, _factory
    url = url or get_settings().database_url
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    _engine = create_engine(url, future=True, **kwargs)
    Base.metadata.create_all(_engine)
    _factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    if _factory is None:
        configure()
    assert _factory is not None
    session = _factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ensure_configured() -> None:
    if _engine is None:
        configure()
