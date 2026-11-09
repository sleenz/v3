from datetime import UTC, date, datetime, timedelta

from app import clock


def test_frozen_by_conftest():
    assert clock.today() == date(2026, 10, 26)
    assert clock.now().utcoffset() == timedelta(hours=8)


def test_freeze_converts_aware_times_to_hkt():
    with clock.freeze(datetime(2026, 10, 31, 20, 0, tzinfo=UTC)):
        assert clock.today() == date(2026, 11, 1)
        assert clock.now().hour == 4
    assert clock.today() == date(2026, 10, 26)


def test_unfrozen_clock_is_hkt():
    clock.set_frozen(None)
    try:
        assert clock.now().tzinfo == clock.TZ
    finally:
        clock.set_frozen(datetime(2026, 10, 26, 9, 0))
