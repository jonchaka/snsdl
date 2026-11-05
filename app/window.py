"""Download window: queued jobs only start between two wall-clock times in TZ."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:   # config imports this module; avoid the cycle at runtime
    from .config import Settings

_WINDOW = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-3]):([0-5][0-9])$")


@dataclass(frozen=True)
class Window:
    start: time
    end: time

    @property
    def label(self) -> str:
        return f"{self.start:%H:%M}–{self.end:%H:%M}"   # en dash

    def contains(self, now: datetime) -> bool:
        t = now.time()
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end   # wraps past midnight

    def next_start(self, now: datetime) -> datetime:
        """now while the window is open, else the next datetime at start (today or tomorrow),
        in now's timezone (wall time, so a DST change in between keeps the clock time)."""
        if self.contains(now):
            return now
        candidate = datetime.combine(now.date(), self.start, tzinfo=now.tzinfo)
        if candidate <= now:
            candidate = datetime.combine(now.date() + timedelta(days=1), self.start, tzinfo=now.tzinfo)
        # A wall time skipped by a spring-forward (02:30 in Berlin on the last Sunday of March)
        # does not exist; the UTC round trip turns it into the real instant (03:30 CEST).
        if now.tzinfo is not None:
            candidate = candidate.astimezone(timezone.utc).astimezone(now.tzinfo)
        return candidate

    def to_dict(self, now: datetime) -> dict:
        active = self.contains(now)
        return {"from": f"{self.start:%H:%M}", "to": f"{self.end:%H:%M}", "active": active,
                "next_start": None if active else self.next_start(now).isoformat()}


def parse_window(raw: str) -> Optional[Window]:
    """'HH:MM-HH:MM' (24 h, may wrap midnight); empty/whitespace -> None (always open)."""
    value = raw.strip()
    if not value:
        return None
    match = _WINDOW.fullmatch(value)
    if not match:
        raise ValueError(f"download window must look like 01:00-07:00 (24 h, 00:00..23:59), got {raw!r}")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    start, end = time(h1, m1), time(h2, m2)
    if start == end:
        raise ValueError(f"download window start and end must differ, got {raw!r}")
    return Window(start, end)


def now_in_tz(settings: "Settings") -> datetime:
    """The current time in settings.tz (UTC if the zone is unknown or empty)."""
    try:
        tz = ZoneInfo(settings.tz)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    return datetime.now(tz)
