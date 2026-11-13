"""Pure value parsers. No I/O, no database: easy to test.

Every parser returns the normalised value, or None when the input is invalid.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

_HEADER_SEP = re.compile(r"[_\s]+")


def norm_header(header: str) -> str:
    """lower-case, trim, then collapse every run of '_' / whitespace to one space."""
    return _HEADER_SEP.sub(" ", header.lower().strip())


# ---------------------------------------------------------------- phone
def parse_phone(raw: str | None) -> str | None:
    """Indian mobile number -> '+91XXXXXXXXXX', or None if invalid."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


# ---------------------------------------------------------------- date
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DMY = re.compile(r"^(\d{1,2})([/-])(\d{1,2})\2(\d{4})$")  # same separator twice
_DMON = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(raw: str | None) -> str | None:
    """YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY (day-first) -> 'YYYY-MM-DD'."""
    if raw is None:
        return None
    s = str(raw).strip()
    try:
        m = _ISO.match(s)
        if m:
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
        elif (m := _DMY.match(s)):
            d, mo, y = int(m[1]), int(m[3]), int(m[4])
        elif (m := _DMON.match(s)):
            mo = _MONTHS.get(m[2].lower())
            if mo is None:
                return None
            d, y = int(m[1]), int(m[3])
        else:
            return None
        return date(y, mo, d).isoformat()  # raises ValueError on impossible dates
    except ValueError:
        return None


# ---------------------------------------------------------------- decimal
_CURRENCY = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_NUMBER = re.compile(r"^(?:\d+\.?\d*|\.\d+)$")


def parse_decimal(raw: str | None) -> str | None:
    """Money amount -> string with exactly 2 decimals (half-up), or None."""
    if raw is None:
        return None
    s = _CURRENCY.sub("", str(raw).strip(), count=1).replace(",", "").strip()
    if not _NUMBER.match(s):  # also rejects negatives, '-', 'N/A', words
        return None
    try:
        return str(Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None


# ---------------------------------------------------------------- enum
def parse_enum(raw: str | None, values) -> str | None:
    if raw is None:
        return None
    v = str(raw).strip().lower()
    return v if v in {x.lower() for x in values} else None


# ---------------------------------------------------------------- timestamps
def parse_timestamp(raw) -> datetime | None:
    """'...Z', '...+05:30' or naive (= IST) -> aware UTC datetime, or None."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s[-1] in "zZ":
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def to_db_ts(dt: datetime) -> str:
    """Fixed-width UTC string: sorts correctly as plain text."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")


def from_db_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=UTC)


def ist_iso(s: str) -> str:
    """DB timestamp -> '2026-10-14T17:55:24+05:30'."""
    return from_db_ts(s).astimezone(IST).isoformat(timespec="seconds")


def ist_date(s: str) -> str:
    """DB timestamp -> IST calendar date 'YYYY-MM-DD'."""
    return from_db_ts(s).astimezone(IST).date().isoformat()
