from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import pytest

from app import clock

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 26)


@pytest.fixture(autouse=True)
def frozen_clock() -> Iterator[None]:
    """Every test runs on Monday 26 Oct 2026, 09:00 Asia/Hong_Kong."""
    with clock.freeze(datetime(2026, 10, 26, 9, 0)):
        yield


@pytest.fixture
def fixture_text():
    def load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return load


@pytest.fixture(autouse=True)
def log_everything():
    """Build every log record, so a bad `extra` field fails in tests, not in production."""
    import logging

    logger = logging.getLogger("grc")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    yield
    logger.setLevel(previous)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Settings read the environment: a developer's exported variables must not leak into tests."""
    from app.settings import Settings, get_settings

    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
