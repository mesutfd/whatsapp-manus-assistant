"""
Quiet-hours utility. Pure functions; reads config from the DB row at call time.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Dict, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _parse_hhmm(value: str) -> Optional[time]:
    if not value:
        return None
    try:
        hh, mm = value.split(":", 1)
        return time(hour=int(hh), minute=int(mm))
    except (ValueError, AttributeError):
        return None


def _now_in_tz(tz_name: str) -> datetime:
    if ZoneInfo is None or not tz_name:
        return datetime.now()
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now()


def is_quiet_now(config: Dict) -> bool:
    """True if 'now' falls inside the configured quiet window."""
    if not config or not config.get("quiet_hours_enabled"):
        return False

    start = _parse_hhmm(config.get("quiet_hours_start") or "")
    end = _parse_hhmm(config.get("quiet_hours_end") or "")
    if start is None or end is None:
        return False

    tz_name = config.get("quiet_hours_timezone") or "UTC"
    now_local = _now_in_tz(tz_name).time()

    if start == end:
        # Treat start==end as "always quiet" so users have an explicit silent mode.
        return True
    if start < end:
        return start <= now_local < end
    # Window crosses midnight, e.g. 22:00 → 08:00.
    return now_local >= start or now_local < end


def status(config: Dict) -> Tuple[bool, str]:
    """Return (is_quiet, human_readable_window)."""
    quiet = is_quiet_now(config)
    if not config.get("quiet_hours_enabled"):
        return quiet, "disabled"
    start = config.get("quiet_hours_start") or ""
    end = config.get("quiet_hours_end") or ""
    tz = config.get("quiet_hours_timezone") or "UTC"
    return quiet, f"{start} → {end} ({tz})"
