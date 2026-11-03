"""Garde-fous : horaires de travail, quotas journaliers/hebdomadaires, montée en charge progressive, pauses aléatoires."""
from __future__ import annotations

import random
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from . import db
from .config import Settings

INVITE_KINDS = ("invite_sent",)
MESSAGE_KINDS = ("message_sent", "inmail_sent")


def local(now: datetime, s: Settings) -> datetime:
    return now.astimezone(ZoneInfo(s.timezone))


def in_working_hours(now: datetime, s: Settings) -> bool:
    t = local(now, s)
    return t.weekday() in s.work_days and s.work_start_hour <= t.hour < s.work_end_hour


def start_of_day(now: datetime, s: Settings) -> datetime:
    return local(now, s).replace(hour=0, minute=0, second=0, microsecond=0)


def start_of_week(now: datetime, s: Settings) -> datetime:
    day = start_of_day(now, s)
    return day - timedelta(days=day.weekday())


def _past_active_days(conn: sqlite3.Connection, now: datetime, s: Settings) -> int:
    """Nombre de jours (avant aujourd'hui) où au moins une invitation a été envoyée."""
    today = start_of_day(now, s).date()
    days = {local(datetime.fromisoformat(r["at"]), s).date()
            for r in conn.execute("SELECT at FROM events WHERE kind = 'invite_sent'")}
    return len({d for d in days if d < today})


def invite_limit_today(conn: sqlite3.Connection, now: datetime, s: Settings) -> int:
    warm = s.warmup_start_invites + s.warmup_step * _past_active_days(conn, now, s)
    return min(s.max_invites_per_day, warm)


def invites_remaining(conn: sqlite3.Connection, now: datetime, s: Settings) -> int:
    today = invite_limit_today(conn, now, s) - db.count_events(conn, INVITE_KINDS, start_of_day(now, s))
    week = s.max_invites_per_week - db.count_events(conn, INVITE_KINDS, start_of_week(now, s))
    return max(0, min(today, week))


def messages_remaining(conn: sqlite3.Connection, now: datetime, s: Settings) -> int:
    return max(0, s.max_messages_per_day - db.count_events(conn, MESSAGE_KINDS, start_of_day(now, s)))


def human_pause(s: Settings, sleep: Callable[[float], None] = time.sleep) -> float:
    delay = random.uniform(s.min_delay_seconds, s.max_delay_seconds)
    sleep(delay)
    return delay
