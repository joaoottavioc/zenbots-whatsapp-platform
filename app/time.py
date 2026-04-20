from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def current_brt_year_month() -> str:
    """Return "YYYY-MM" for the current instant in BRT (UTC-3, no DST).

    Used to key per-calendar-month rows (pricing usage counters, billing
    cycles). Brazilian customers think in BRT calendar boundaries, so the
    month rollover must happen at midnight BRT, not midnight UTC.
    """
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m")
