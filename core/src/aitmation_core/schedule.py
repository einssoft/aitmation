"""Time as a trigger source (Stufe 1.3): cases that start from the calendar.

A template whose trigger declares ``source = "schedule"`` runs on a
rhythm the declaration spells out in plain words - no cron syntax for a
business without an IT department::

    [steps.start]
    kind = "trigger"
    config = { source = "schedule", every = "month", day = 1, at = "08:00" }

``every`` is ``day`` | ``week`` | ``month``; ``at`` is a local wall-clock
time (``HH:MM``, default 08:00); ``weekday`` (1 = Monday to 7 = Sunday)
applies to weeks, ``day`` (1-28, so every month has it) to months. The
runtime keeps one row per template in ``schedules`` (migration 0009):
the next planned start and the last actual one. A missed slot (the
machine was off) fires once on the next tick, not once per missed day -
the person sees one case, not a backlog. Times are local: a monthly
close at 08:00 means 08:00 where the business sits.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

EVERY = ("day", "week", "month")


class ScheduleError(ValueError):
    """The schedule declaration is invalid."""


@dataclass(frozen=True)
class Schedule:
    every: str
    hour: int = 8
    minute: int = 0
    weekday: int = 1  # ISO: 1 = Monday
    day: int = 1  # day of month, 1..28

    def describe(self) -> dict:
        return {
            "every": self.every,
            "at": f"{self.hour:02d}:{self.minute:02d}",
            "weekday": self.weekday if self.every == "week" else None,
            "day": self.day if self.every == "month" else None,
        }

    def next_after(self, moment: datetime) -> datetime:
        """The first planned start strictly after ``moment`` (local time)."""
        local = moment.astimezone()
        candidate = local.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if self.every == "day":
            if candidate <= local:
                candidate += timedelta(days=1)
            return candidate
        if self.every == "week":
            shift = (self.weekday - candidate.isoweekday()) % 7
            candidate += timedelta(days=shift)
            if candidate <= local:
                candidate += timedelta(days=7)
            return candidate
        # month
        candidate = candidate.replace(day=self.day)
        if candidate <= local:
            year, month = candidate.year, candidate.month + 1
            if month > 12:
                year, month = year + 1, 1
            candidate = candidate.replace(year=year, month=month)
        return candidate


def parse_schedule(config: dict, *, source: str = "trigger") -> Schedule:
    every = str(config.get("every", "")).lower()
    if every not in EVERY:
        raise ScheduleError(f"{source}: every must be one of {EVERY}, got {every!r}")
    at = str(config.get("at", "08:00"))
    try:
        hour_text, minute_text = at.split(":")
        hour, minute = int(hour_text), int(minute_text)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError as exc:
        raise ScheduleError(f"{source}: at must be HH:MM, got {at!r}") from exc
    weekday = _weekday(config.get("weekday", 1), source)
    try:
        day = int(config.get("day", 1))
        if not 1 <= day <= 28:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ScheduleError(f"{source}: day must be 1 to 28 so every month has it") from exc
    return Schedule(every=every, hour=hour, minute=minute, weekday=weekday, day=day)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def due(conn: sqlite3.Connection, template_id: str, schedule: Schedule, now: datetime) -> bool:
    """Whether the template's next slot has passed; plans the first slot on
    first sight (never retroactively)."""
    row = conn.execute(
        "SELECT next_at FROM schedules WHERE template_id = ?", (template_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schedules (template_id, next_at) VALUES (?, ?)",
            (template_id, _iso(schedule.next_after(now))),
        )
        conn.commit()
        return False
    return datetime.fromisoformat(row["next_at"]) <= now.astimezone(UTC)


def mark_fired(
    conn: sqlite3.Connection, template_id: str, schedule: Schedule, now: datetime
) -> None:
    conn.execute(
        "UPDATE schedules SET last_fired_at = ?, next_at = ? WHERE template_id = ?",
        (_iso(now), _iso(schedule.next_after(now)), template_id),
    )
    conn.commit()


def state(conn: sqlite3.Connection, template_id: str) -> dict:
    row = conn.execute(
        "SELECT next_at, last_fired_at FROM schedules WHERE template_id = ?", (template_id,)
    ).fetchone()
    return dict(row) if row else {"next_at": None, "last_fired_at": None}


_WEEKDAYS = {
    "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5,
    "saturday": 6, "sunday": 7,
    "montag": 1, "dienstag": 2, "mittwoch": 3, "donnerstag": 4, "freitag": 5,
    "samstag": 6, "sonntag": 7,
    "lundi": 1, "mardi": 2, "mercredi": 3, "jeudi": 4, "vendredi": 5, "samedi": 6, "dimanche": 7,
    "lunedì": 1, "martedì": 2, "mercoledì": 3, "giovedì": 4, "venerdì": 5, "sabato": 6,
    "domenica": 7,
}


def _weekday(value, source: str) -> int:
    """ISO number or a weekday name in one of the four languages."""
    if isinstance(value, str) and value.strip().lower() in _WEEKDAYS:
        return _WEEKDAYS[value.strip().lower()]
    try:
        number = int(value)
        if not 1 <= number <= 7:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ScheduleError(
            f"{source}: weekday must be 1 (Monday) to 7 (Sunday) or a weekday name"
        ) from exc
    return number
