"""Date handling for values read from the Sheet. Sheets are read unformatted,
so real date cells arrive as serial numbers (days since 1899-12-30) and
text cells arrive as typed."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SHEETS_EPOCH = datetime(1899, 12, 30)
TEXT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d")


def parse_sheet_datetime(value: str, tz: ZoneInfo) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        serial = float(value)
    except ValueError:
        serial = None
    if serial is not None:
        # Guard against small numbers that aren't dates (e.g. someone typed "15").
        return (SHEETS_EPOCH + timedelta(days=serial)).replace(tzinfo=tz) if serial > 30000 else None
    for fmt in TEXT_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def fmt_time(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    return f"{dt:%a %d %b}, {hour}:{dt:%M} {'am' if dt.hour < 12 else 'pm'}"


def display_value(value: str, tz: ZoneInfo) -> str:
    """Human-readable form of a cell that may hold a date (e.g. a deadline)."""
    dt = parse_sheet_datetime(value, tz)
    if dt is None:
        return (value or "").strip()
    if dt.hour == 0 and dt.minute == 0:
        return f"{dt:%a %d %b}"
    return fmt_time(dt)


def now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def stamp(tz: ZoneInfo) -> str:
    return now(tz).strftime("%Y-%m-%d %H:%M")
