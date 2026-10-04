from datetime import datetime
from zoneinfo import ZoneInfo

import app as app_module
from app import csv_time_to_local


def test_named_timezone_follows_daylight_saving(monkeypatch):
    monkeypatch.setattr(app_module, 'LOCAL_TZ', ZoneInfo('America/Los_Angeles'))
    assert csv_time_to_local(datetime(2026, 10, 4, 7, 1)) == datetime(2026, 10, 4, 0, 1)   # PDT, UTC-7
    assert csv_time_to_local(datetime(2026, 12, 4, 7, 1)) == datetime(2026, 12, 3, 23, 1)  # PST, UTC-8


def test_named_timezone_crosses_new_year(monkeypatch):
    monkeypatch.setattr(app_module, 'LOCAL_TZ', ZoneInfo('America/New_York'))
    assert csv_time_to_local(datetime(2027, 1, 1, 2, 0)) == datetime(2026, 12, 31, 21, 0)


def test_fixed_offset_fallback(monkeypatch):
    monkeypatch.setattr(app_module, 'LOCAL_TZ', None)
    monkeypatch.setattr(app_module, 'TIMEZONE_OFFSET', 5)
    assert csv_time_to_local(datetime(2026, 10, 4, 7, 1)) == datetime(2026, 10, 4, 2, 1)
    monkeypatch.setattr(app_module, 'TIMEZONE_OFFSET', 0)
    assert csv_time_to_local(datetime(2026, 10, 4, 7, 1)) == datetime(2026, 10, 4, 7, 1)
