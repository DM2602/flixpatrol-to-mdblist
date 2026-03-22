"""
Lightweight cron-expression scheduler.

Supports standard 5-field cron:  minute hour day-of-month month day-of-week
Also supports common shortcuts like @daily, @hourly, @weekly.

Examples:
    "0 6 * * *"        → every day at 06:00
    "0 6,18 * * *"     → every day at 06:00 and 18:00
    "0 */6 * * *"      → every 6 hours
    "30 2 * * 1"       → every Monday at 02:30
    "@daily"            → 00:00 every day
    "@hourly"           → every hour at :00
"""

import re
import time
from datetime import datetime, timezone


SHORTCUTS = {
    "@yearly":   "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly":  "0 0 1 * *",
    "@weekly":   "0 0 * * 0",
    "@daily":    "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly":   "0 * * * *",
}


class CronField:
    """Parses one cron field into a set of valid integer values."""

    def __init__(self, expr: str, lo: int, hi: int):
        self.values: set[int] = set()
        for part in expr.split(","):
            part = part.strip()
            if part == "*":
                self.values.update(range(lo, hi + 1))
            elif part.startswith("*/"):
                step = int(part[2:])
                self.values.update(range(lo, hi + 1, step))
            elif "-" in part:
                if "/" in part:
                    rng, step = part.split("/")
                    a, b = rng.split("-")
                    self.values.update(range(int(a), int(b) + 1, int(step)))
                else:
                    a, b = part.split("-")
                    self.values.update(range(int(a), int(b) + 1))
            else:
                self.values.add(int(part))

    def matches(self, value: int) -> bool:
        return value in self.values


class CronExpression:
    """Parsed 5-field cron expression."""

    def __init__(self, expr: str):
        expr = SHORTCUTS.get(expr.strip().lower(), expr).strip()
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(
                f"Cron expression must have 5 fields (got {len(parts)}): {expr}"
            )
        self.minute = CronField(parts[0], 0, 59)
        self.hour = CronField(parts[1], 0, 23)
        self.dom = CronField(parts[2], 1, 31)
        self.month = CronField(parts[3], 1, 12)
        self.dow = CronField(parts[4], 0, 6)  # 0=Sun

    def matches(self, dt: datetime) -> bool:
        return (
            self.minute.matches(dt.minute)
            and self.hour.matches(dt.hour)
            and self.dom.matches(dt.day)
            and self.month.matches(dt.month)
            and self.dow.matches(dt.weekday() + 1 if dt.weekday() < 6 else 0)
            # Python weekday: Mon=0..Sun=6  →  cron: Sun=0..Sat=6
        )


class Scheduler:
    """
    Computes when to run next based on a cron expression.

    Usage:
        s = Scheduler("0 6,18 * * *")
        s.seconds_until_next()   # seconds to sleep
        s.next_run_str()         # human-readable next run
        s.advance()              # call after a run completes
    """

    def __init__(self, cron_expr: str):
        self.cron = CronExpression(cron_expr)
        self._next: datetime | None = None
        self._compute_next()

    def _compute_next(self):
        now = datetime.now()
        # Start from next minute
        candidate = now.replace(second=0, microsecond=0)
        candidate = candidate + __import__("datetime").timedelta(minutes=1)

        # Walk forward minute by minute for up to 366 days
        limit = 60 * 24 * 366
        for _ in range(limit):
            if self.cron.matches(candidate):
                self._next = candidate
                return
            candidate += __import__("datetime").timedelta(minutes=1)

        # Shouldn't happen with a valid expression
        self._next = now + __import__("datetime").timedelta(hours=1)

    def next_run(self) -> datetime:
        if self._next is None:
            self._compute_next()
        return self._next

    def next_run_str(self) -> str:
        return self.next_run().strftime("%Y-%m-%d %H:%M")

    def seconds_until_next(self) -> float:
        delta = (self.next_run() - datetime.now()).total_seconds()
        return max(delta, 0)

    def advance(self):
        """Re-compute next run time after a run has finished."""
        self._next = None
        self._compute_next()
