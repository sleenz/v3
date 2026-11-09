"""Single source of "now" in Asia/Hong_Kong. Tests freeze it with `freeze()`."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Hong_Kong")

_frozen: datetime | None = None


def now() -> datetime:
    """Current time, timezone-aware, in Asia/Hong_Kong."""
    if _frozen is not None:
        return _frozen
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def set_frozen(moment: datetime | None) -> None:
    """Freeze the clock at `moment` (naive values are taken as HKT); None unfreezes."""
    global _frozen
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=TZ)
    _frozen = moment.astimezone(TZ) if moment is not None else None


@contextmanager
def freeze(moment: datetime) -> Iterator[None]:
    previous = _frozen
    set_frozen(moment)
    try:
        yield
    finally:
        set_frozen(previous)
