#!/usr/bin/env python3
"""
A reminder tool, to show off how fireaid reads values by their type hints.

Fire alone takes every value for a Python literal where it can: the
title 007 becomes the number 7, the date 2026-10-15 stays a string, and
1e3 is a float wherever it appears. fireaid gives each parameter what
its hint says, or a usage error naming the parameter.

Run examples/hints_tour.sh to see all of the below, or try:

    reminders.py add 007 2026-10-15
    reminders.py add "Water the plants" 2026-10-15 --at 08:30
    reminders.py add Stand-up 2026-10-19 --at 09:15 --every 1d --priority HIGH
    reminders.py add Call 15.10.2026
    reminders.py snooze 0x3 --by 1h30m
    reminders.py help add
"""

# Annotations as strings, as in many programs: fireaid reads them all the same.
from __future__ import annotations

import datetime
import enum
from typing import Optional

import fireaid as fire


class Priority(enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class Reminders:
    """Keep reminders, as far as saying what they would be."""

    def add(self, title: str, on: datetime.date, at: datetime.time = datetime.time(9),
            every: Optional[datetime.timedelta] = None, priority: Priority = Priority.NORMAL):
        """Add a reminder.

        Args:
            title: What to be reminded of, as typed.
            on: The day, as YYYY-MM-DD.
            at: The time of day, as HH:MM.
            every: How often to repeat it, as 1d, 1h30m or a number of seconds.
            priority: low, normal or high.
        """
        when = datetime.datetime.combine(on, at)
        repeat = f", every {every}" if every else ""
        return f"{priority.value} priority: {title!r} on {when:%A %Y-%m-%d at %H:%M}{repeat}"

    def snooze(self, number: int, by: datetime.timedelta = datetime.timedelta(minutes=10)):
        """Put a reminder off.

        Args:
            number: Which reminder, as 3, or 0x3 for that matter.
            by: How long for, as 10m, 1h30m or a number of seconds.
        """
        return f"reminder {number} put off by {by}"


if __name__ == "__main__":
    fire.Fire(Reminders())
