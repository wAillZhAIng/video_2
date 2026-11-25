"""Store an uploaded original and its database records."""

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from time import sleep
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.errors import AppError


T = TypeVar("T")


def _sqlite_write_conflict(error: OperationalError) -> bool:
    cause = error.orig
    if not isinstance(cause, sqlite3.OperationalError):
        return False
    code = getattr(cause, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    return str(cause).split(":", 1)[0] in {
        "database is locked", "database table is locked", "database schema is locked",
    }


def persist_upload(session: Session, destination: Path, data: bytes, stage: Callable[[], T]) -> T:
    """Write a unique original once. Stage only database changes on each attempt."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = committed = False
    try:
        with destination.open("xb") as original:
            created = True
            original.write(data)

        for delay in (0.1, 0.25, None):
            try:
                result = stage()
                session.commit()
            except OperationalError as exc:
                session.rollback()
                if not _sqlite_write_conflict(exc):
                    raise
                if delay is None:
                    raise AppError(503, "import_database_busy", "数据库正在保存其他任务，请稍后重试导入。",
                                   {"retryable": True}) from exc
                sleep(delay)
            else:
                committed = True
                return result
    finally:
        if created and not committed:
            try:
                session.rollback()
            finally:
                destination.unlink(missing_ok=True)
                with suppress(OSError):
                    destination.parent.rmdir()
