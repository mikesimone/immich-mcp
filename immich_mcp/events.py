"""Event albums: pure logic for file_event_pictures (time window, camera rules, album numbering).

The tool files every photo/video the owner's own cameras took during an event (e.g. a calendar entry) into a new
numbered album. Selection deliberately looks past the album allow-list -- the owner's camera shots usually sit in no
album or in hidden ones -- but only for assets matching EVENT_CAMERA_RULES inside the event's time window, and the
result always lands in an allowed album. The exclude list still wins.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class EventError(ValueError):
    pass


RULE_KEYS = {"device", "exif_make", "exif_model_prefix", "filename_contains", "filename_prefix"}


def parse_rules(raw: str) -> tuple[dict, ...]:
    """EVENT_CAMERA_RULES: JSON list of {"device": name, <one or more matchers>}; all matchers must hold."""
    if not raw.strip():
        return ()
    rules = json.loads(raw)
    if not isinstance(rules, list):
        raise EventError("EVENT_CAMERA_RULES must be a JSON list")
    for r in rules:
        if not isinstance(r, dict) or not r.get("device") or set(r) - RULE_KEYS or len(set(r) - {"device"}) == 0:
            raise EventError(f"bad camera rule {r!r}: needs device plus one of {sorted(RULE_KEYS - {'device'})}")
    return tuple(rules)


def device_of(asset: dict, rules: tuple[dict, ...]) -> str | None:
    ex = asset.get("exifInfo") or {}
    make = (ex.get("make") or "").strip().lower()
    model = (ex.get("model") or "").strip().lower()
    name = os.path.basename(asset.get("originalFileName") or "").lower()
    for r in rules:
        checks = []
        if "exif_make" in r:
            checks.append(make == r["exif_make"].lower())
        if "exif_model_prefix" in r:
            checks.append(model.startswith(r["exif_model_prefix"].lower()))
        if "filename_contains" in r:
            checks.append(r["filename_contains"].lower() in name)
        if "filename_prefix" in r:
            checks.append(name.startswith(r["filename_prefix"].lower()))
        if checks and all(checks):
            return r["device"]
    return None


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _bound(s: str, tz: ZoneInfo) -> tuple[datetime, bool]:
    s = s.strip()
    if _DATE.fullmatch(s):
        return datetime.fromisoformat(s), True
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise EventError(f"not an ISO date or date-time: {s!r}") from None
    if d.tzinfo is not None:
        d = d.astimezone(tz).replace(tzinfo=None)
    return d, False


def window(start: str, end: str, tz: ZoneInfo, pad_minutes: int) -> tuple[datetime, datetime, bool]:
    """Wall-clock [start, end) in tz. Timed events are padded both sides; all-day events (dates, end exclusive as
    calendars give it) are not."""
    s, s_day = _bound(start, tz)
    e, e_day = _bound(end, tz)
    if s_day != e_day:
        raise EventError("start and end must both be dates (all-day) or both be date-times")
    if not s_day:
        s, e = s - timedelta(minutes=pad_minutes), e + timedelta(minutes=pad_minutes)
    if e <= s:
        raise EventError("event end is not after its start")
    if e - s > timedelta(days=31):
        raise EventError("event longer than 31 days; split it")
    return s, e, s_day


def local_taken(asset: dict) -> datetime | None:
    """Immich's localDateTime is the wall clock where the shot was taken, written as if UTC. Comparing wall clocks is
    right for cameras that write no zone offset (smart glasses, spy cams), which Immich would otherwise place hours off."""
    v = asset.get("localDateTime") or asset.get("fileCreatedAt")
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)


def album_number(name: str, prefix: str) -> int | None:
    m = re.match(r"^" + re.escape(prefix) + r"(\d{3})(?=\s|$)", name)
    return int(m.group(1)) if m else None


def pick_album(albums: list[dict], prefix: str, title: str, lo: int = 1, hi: int = 999) -> tuple[dict | None, str]:
    """-> (existing album with exactly this title, or None; album name). New numbers are the highest PREFIXnnn inside
    [lo, hi] + 1 (or lo), so an owner can keep events in one block (e.g. 010.100-199) beside other 010. albums."""
    want = title.strip().lower()
    used = []
    for a in albums:
        n = album_number(a["albumName"], prefix)
        if n is None:
            continue
        if lo <= n <= hi:
            used.append(n)
        rest = re.sub(r"^\d{3}\s*-?\s*", "", a["albumName"][len(prefix):]).strip().lower()
        if rest == want:
            return a, a["albumName"]
    n = max(used) + 1 if used else lo
    if n > hi:
        raise EventError(f"no free numbers left in {prefix}{lo:03d}-{prefix}{hi:03d}")
    return None, f"{prefix}{n:03d} - {title.strip()}"
