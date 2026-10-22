from datetime import date


def parse_date(value):
    return date.fromisoformat(value)
