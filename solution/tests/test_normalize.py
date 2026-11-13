import pytest

from pipeline.normalize import (ist_date, ist_iso, norm_header, parse_date, parse_decimal,
                                parse_enum, parse_phone, parse_timestamp, to_db_ts)


@pytest.mark.parametrize("raw", [
    "9876543210", "+91 98765 43210", "09876543210", "91-98765-43210", "(+91) 9876543210",
    "+919876543210", "  98765 43210 ",
])
def test_phone_valid_formats(raw):
    assert parse_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",            # 9 digits
    "5876543210",           # bad prefix
    "022-2345-6789",        # landline
    "N/A", "", None,
    "+1 415 555 0134",
    "98765432101",          # 11 digits not starting with 0
    "0919876543210",        # 13 digits
    "+91 0987654321",       # 91 + leading 0 -> 11 digits after strip, not valid
])
def test_phone_invalid(raw):
    assert parse_phone(raw) is None


def test_phone_91_prefix_only_dropped_when_12_digits():
    # 10-digit number that happens to start with 91 must stay intact
    assert parse_phone("9198765432") == "+919198765432"


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", "2026-12-01"),
    ("01/12/2026", "2026-12-01"),      # day-first
    ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"),
    ("01-DEC-2026", "2026-12-01"),
    ("1-dec-2026", "2026-12-01"),
    ("29/02/2028", "2028-02-29"),      # leap year
    (" 05/03/2026 ", "2026-03-05"),
])
def test_date_valid(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "31/02/2026", "29/02/2027", "2026-13-01", "soon", "TBD", "", "13/13/2026",
    "01-Foo-2026", "2026/12/01", "01/12-2026", "12/31/2026",
])
def test_date_invalid(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("1200", "1200.00"), ("12000.5", "12000.50"), ("₹12,345", "12345.00"),
    ("Rs. 24787", "24787.00"), ("Rs 5", "5.00"), ("rs.5", "5.00"), ("INR 18678.3", "18678.30"),
    ("inr1,00,000.456", "100000.46"), ("0", "0.00"), ("  ₹ 1,000  ", "1000.00"),
])
def test_decimal_valid(raw, expected):
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["N/A", "-", "twelve thousand", "-5", "", "Rs.", "1.2.3", "$5"])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


def test_decimal_rounds_half_up():
    assert parse_decimal("0.125") == "0.13"


def test_enum_case_insensitive_stored_lowercase():
    vals = ["en", "hi", "ta"]
    assert parse_enum("HI", vals) == "hi"
    assert parse_enum(" ta ", vals) == "ta"
    assert parse_enum("Hindi", vals) is None


def test_header_normalisation():
    assert norm_header("  Policy_No ") == "policy no"
    assert norm_header("POLICY   NO") == "policy no"
    assert norm_header("policy__no") == "policy no"
    assert norm_header("policy_ no") == "policy no"


def test_timestamps_three_forms_and_ist_dates():
    utc = parse_timestamp("2026-10-03T19:00:00Z")
    off = parse_timestamp("2026-10-04T00:30:00+05:30")
    naive = parse_timestamp("2026-10-04 00:30:00")   # naive means IST
    assert utc == off == naive
    # 19:00Z is already the next day in IST
    assert ist_date(to_db_ts(utc)) == "2026-10-04"
    assert ist_iso(to_db_ts(utc)) == "2026-10-04T00:30:00+05:30"


def test_timestamp_garbage():
    assert parse_timestamp("yesterday") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp(12345) is None
