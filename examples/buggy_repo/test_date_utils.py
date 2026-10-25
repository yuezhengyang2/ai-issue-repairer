from datetime import date

from date_utils import parse_date


def test_empty_date():
    assert parse_date("") is None


def test_valid_date():
    assert parse_date("2026-10-08") == date(2026, 10, 8)
