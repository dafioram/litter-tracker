from datetime import datetime

import pytest

from app import export_date_from_filename, parse_whisker_timestamp


def test_export_date_from_filename():
    assert export_date_from_filename('litter-robot_4_activity_2026-10-04.csv') == datetime(2026, 10, 4)
    assert export_date_from_filename('02001113-litter-robot_4_activity_2026-10-04.csv') == datetime(2026, 10, 4)
    assert export_date_from_filename('export.csv') is None
    assert export_date_from_filename(None) is None


def test_same_year():
    assert parse_whisker_timestamp('10/4 7:01 am', datetime(2026, 10, 6)) == datetime(2026, 10, 4, 7, 1)
    assert parse_whisker_timestamp('9/7 11:23 am', datetime(2026, 10, 6)) == datetime(2026, 9, 7, 11, 23)


def test_am_pm():
    latest = datetime(2026, 10, 6)
    assert parse_whisker_timestamp('10/3 12:01 pm', latest) == datetime(2026, 10, 3, 12, 1)
    assert parse_whisker_timestamp('10/3 12:01 am', latest) == datetime(2026, 10, 3, 0, 1)
    assert parse_whisker_timestamp('10/3 7:39 pm', latest) == datetime(2026, 10, 3, 19, 39)


def test_january_export_keeps_december_in_previous_year():
    latest = datetime(2026, 1, 7)  # exported 2026-01-05
    assert parse_whisker_timestamp('12/20 6:43 am', latest) == datetime(2025, 12, 20, 6, 43)
    assert parse_whisker_timestamp('1/2 6:43 am', latest) == datetime(2026, 1, 2, 6, 43)


def test_new_years_eve_export_allows_next_day():
    latest = datetime(2027, 1, 2)  # exported 2026-12-31
    assert parse_whisker_timestamp('12/31 11:00 pm', latest) == datetime(2026, 12, 31, 23, 0)
    assert parse_whisker_timestamp('1/1 2:00 am', latest) == datetime(2027, 1, 1, 2, 0)


def test_leap_day():
    assert parse_whisker_timestamp('2/29 8:00 am', datetime(2028, 3, 2)) == datetime(2028, 2, 29, 8, 0)
    with pytest.raises(ValueError):
        parse_whisker_timestamp('2/29 8:00 am', datetime(2026, 3, 2))


def test_garbage_raises():
    with pytest.raises((ValueError, IndexError)):
        parse_whisker_timestamp('not a date', datetime(2026, 10, 6))
