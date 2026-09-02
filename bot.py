#!/usr/bin/env python3
"""
Avails Bot — weekly availability collection for the SH livechat team.

One message per day is posted to the group. Each has tap-to-claim buttons and
rewrites itself in place as agents claim slots, so the board is always current
instead of buried under copy-pasted replies.

Setup: follow SETUP.md step by step. README.md is the reference guide.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    MenuButtonWebApp,
    WebAppInfo,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    constants,
)
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()


def env_int(name: str, default: int = 0) -> int:
    """Treat a blank variable the same as an unset one — hosting panels set blanks."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(
            f"{name} must be a whole number (got {raw!r}). "
            "Check for stray spaces or quotes in your config."
        )


# Left unset on first run: start the bot, add it to the group, send /chatid there,
# then put the number in your env and restart.
GROUP_CHAT_ID = env_int("GROUP_CHAT_ID")
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
TZ = ZoneInfo(os.environ.get("TZ_NAME", "").strip() or "Asia/Singapore")
DB_PATH = os.environ.get("DB_PATH", "").strip() or "avails.db"
MIN_PER_SLOT = env_int("MIN_PER_SLOT", 1)
# How many agents can hold the same slot. 1 = first come, first served.
SLOT_CAPACITY = max(1, env_int("SLOT_CAPACITY", 1))
SHIFT_REMINDER_HOUR = env_int("SHIFT_REMINDER_HOUR", 20)
# Group post tagging whoever is on duty tomorrow, e.g. "18:30".
SHIFT_CALL_TIME = os.environ.get("SHIFT_CALL_TIME", "").strip() or "18:30"
# Personal DMs the evening before. Off by default so nobody is pinged twice.
DM_REMINDERS = os.environ.get("DM_REMINDERS", "").strip().lower() in {"1", "true", "yes", "on"}

# Your team numbers weeks one behind ISO: the Monday of 10 Aug 2026 is ISO week
# 33, but you label it W32. Set to 0 if you ever switch to plain ISO numbering.
WEEK_NUM_OFFSET = env_int("WEEK_NUM_OFFSET", -1)
# Fallback hourly rate in cents, used until you set one with /setrate.
DEFAULT_RATE_CENTS = env_int("DEFAULT_RATE_CENTS", 0)
# How long after a shift ends before an open clock-in is auto-closed.
AUTO_CLOSE_GRACE_MIN = env_int("AUTO_CLOSE_GRACE_MIN", 120)
# Automatic database backup, DM'd to owners. 0-6 = Mon-Sun.
BACKUP_DAY = env_int("BACKUP_DAY", 0)
BACKUP_TIME = os.environ.get("BACKUP_TIME", "").strip() or "09:00"
# "slot"   = pay the full rostered block, however long they were actually on
# "actual" = pay the exact clocked time
PAY_MODE = (os.environ.get("PAY_MODE", "").strip().lower() or "slot")
# Allowed early finish, in minutes, before a slot stops counting as worked.
SLOT_GRACE_MIN = env_int("SLOT_GRACE_MIN", 15)
# Forum topics. Leave blank for a normal group. Get the number by sending
# /chatid inside the topic you want.
GROUP_THREAD_ID = env_int("GROUP_THREAD_ID") or None
# The operations group that gets [OPENING] posts on clock-in, e.g. "TC Online".
# Separate from the avails group. Leave blank and the bot just hands the agent
# the text to copy instead.
OPS_CHAT_ID = env_int("OPS_CHAT_ID")
OPS_THREAD_ID = env_int("OPS_THREAD_ID") or None
# Mini App. PUBLIC_URL comes from Railway once you generate a domain.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
WEB_PORT = env_int("PORT", 8080)
# Optional second topic for the daily on-duty tags. Falls back to the board topic.
SHIFTCALL_THREAD_ID = env_int("SHIFTCALL_THREAD_ID") or GROUP_THREAD_ID

# "single" = one pinned message holding the whole week (neater).
# "daily"  = one message per day (buttons sit closer to their day).
BOARD_MODE = (os.environ.get("BOARD_MODE", "").strip().lower() or "single")
# "on" keeps tap-to-claim buttons on the group board. "off" makes the group
# board a clean read-only summary and moves all claiming into /plan.
GROUP_BUTTONS = (os.environ.get("GROUP_BUTTONS", "").strip().lower() or "on") != "off"
# Short how-to posted under a new board. Set to "off" once the team knows the drill.
POST_INTRO = (os.environ.get("POST_INTRO", "").strip().lower() or "on") != "off"

DAY_NAMES = ["MON", "TUE", "WED", "THURS", "FRI", "SAT", "SUN"]
DAY_ABBR = {"MON": "M", "TUE": "T", "WED": "W", "THURS": "Th",
            "FRI": "F", "SAT": "Sa", "SUN": "Su"}

# Your standard pattern: 5 blocks weekdays, 4 on the weekend.
DEFAULT_SLOTS = {
    "MON": ["10am-12pm", "12pm-2pm", "2pm-4pm", "4pm-6pm", "6pm-8pm"],
    "TUE": ["10am-12pm", "12pm-2pm", "2pm-4pm", "4pm-6pm", "6pm-8pm"],
    "WED": ["10am-12pm", "12pm-2pm", "2pm-4pm", "4pm-6pm", "6pm-8pm"],
    "THURS": ["10am-12pm", "12pm-2pm", "2pm-4pm", "4pm-6pm", "6pm-8pm"],
    "FRI": ["10am-12pm", "12pm-2pm", "2pm-4pm", "4pm-6pm", "6pm-8pm"],
    "SAT": ["10am-12pm", "12pm-2pm", "2pm-4pm", "4pm-6pm"],
    "SUN": ["10am-12pm", "12pm-2pm", "2pm-4pm", "4pm-6pm"],
}

# Campaign weeks (11.11, 12.12, mega sales): every day extended to midnight.
CAMPAIGN_SLOTS = {
    d: ["10am-12pm", "12pm-2pm", "2pm-4pm", "4pm-6pm",
        "6pm-8pm", "8pm-10pm", "10pm-12am"]
    for d in DAY_NAMES
}

SEED_PRESETS = {"standard": DEFAULT_SLOTS, "campaign": CAMPAIGN_SLOTS}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s — %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("availsbot")

write_lock = asyncio.Lock()

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS weeks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT NOT NULL,
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    key_events    TEXT,
    deadline      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    header_msg_id INTEGER
);
CREATE TABLE IF NOT EXISTS days (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    week_id   INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    name      TEXT NOT NULL,
    the_date  TEXT NOT NULL,
    msg_id    INTEGER
);
CREATE TABLE IF NOT EXISTS slots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day_id     INTEGER NOT NULL REFERENCES days(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    label      TEXT NOT NULL,
    start_min  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signups (
    slot_id  INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL,
    name     TEXT NOT NULL,
    ts       TEXT NOT NULL,
    PRIMARY KEY (slot_id, user_id)
);
CREATE TABLE IF NOT EXISTS agents (
    user_id  INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    username TEXT,
    dm_ok    INTEGER NOT NULL DEFAULT 0,
    active   INTEGER NOT NULL DEFAULT 1,
    role     TEXT NOT NULL DEFAULT 'agent',
    status   TEXT NOT NULL DEFAULT 'active',
    display_name TEXT,
    requested_at TEXT,
    decided_by   INTEGER,
    decided_at   TEXT
);
CREATE TABLE IF NOT EXISTS presets (
    name   TEXT PRIMARY KEY,
    config TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS pay_rates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id       INTEGER,
    cents          INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    created_by     INTEGER,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS time_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   INTEGER NOT NULL,
    slot_id    INTEGER,
    the_date   TEXT NOT NULL,
    clock_in   TEXT NOT NULL,
    clock_out  TEXT,
    status     TEXT NOT NULL DEFAULT 'open',
    source     TEXT NOT NULL DEFAULT 'agent',
    note       TEXT
);
CREATE TABLE IF NOT EXISTS time_edits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id   INTEGER NOT NULL,
    changed_by INTEGER NOT NULL,
    before     TEXT,
    after      TEXT,
    reason     TEXT,
    changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmations (
    week_id INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    PRIMARY KEY (week_id, user_id)
);
"""

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(SCHEMA)
_cols = {r[1] for r in db.execute("PRAGMA table_info(agents)")}
if "role" not in _cols:
    db.execute(
        "ALTER TABLE agents ADD COLUMN role TEXT NOT NULL DEFAULT 'agent'"
    )
for _col, _ddl in [
    ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ("display_name", "TEXT"),
    ("support_name", "TEXT"),
    ("on_avails", "INTEGER NOT NULL DEFAULT 1"),
    ("requested_at", "TEXT"),
    ("decided_by", "INTEGER"),
    ("decided_at", "TEXT"),
]:
    if _col not in _cols:
        db.execute(f"ALTER TABLE agents ADD COLUMN {_col} {_ddl}")
for _name, _cfg in SEED_PRESETS.items():
    db.execute(
        "INSERT OR IGNORE INTO presets (name, config) VALUES (?,?)",
        (_name, json.dumps(_cfg)),
    )
db.commit()


def q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return db.execute(sql, args).fetchall()


def q1(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    return db.execute(sql, args).fetchone()


def run(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    cur = db.execute(sql, args)
    db.commit()
    return cur


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def esc(text: str) -> str:
    """Names go into HTML messages, so & < > must be escaped."""
    return html.escape(text or "", quote=False)


def verify_init_data(raw: str) -> dict | None:
    """Prove a Mini App request really came from Telegram.

    Telegram signs the launch data with a key derived from the bot token. We
    recompute that signature; if it matches, the user id can be trusted. Without
    this check anyone could edit the id in their browser and read a colleague's pay.
    """
    if not raw:
        return None
    try:
        pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True)
    except Exception:
        return None
    data = dict(pairs)
    got = data.pop("hash", None)
    if not got:
        return None

    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return None

    # Reject anything older than a day, so a copied link can't be replayed.
    try:
        if int(data.get("auth_date", 0)) < int(now().timestamp()) - 86400:
            return None
    except ValueError:
        return None

    try:
        return json.loads(data.get("user", "{}"))
    except json.JSONDecodeError:
        return None


async def post_ops(bot, text: str) -> bool:
    """Post to the operations group. Returns False if it couldn't."""
    if not OPS_CHAT_ID:
        return False
    kw = {}
    if OPS_THREAD_ID:
        kw["message_thread_id"] = OPS_THREAD_ID
    try:
        await bot.send_message(OPS_CHAT_ID, text, **kw)
        return True
    except Exception as e:
        log.warning("Couldn't post to the ops group: %s", e)
        return False


async def send_group(bot, text: str, thread: int | None = -1, **kw):
    """Post to the group, into the right forum topic if one is configured."""
    tid = GROUP_THREAD_ID if thread == -1 else thread
    if tid:
        kw["message_thread_id"] = tid
    return await bot.send_message(GROUP_CHAT_ID, text, **kw)


def now() -> datetime:
    return datetime.now(TZ)


def parse_time_token(tok: str) -> int:
    """'10am' / '2:30pm' / '18:00' -> minutes since midnight."""
    tok = tok.strip().lower().replace(" ", "").replace(".", ":")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", tok)
    if not m:
        raise ValueError(f"Could not read the time {tok!r}")
    hour, minute, mer = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if mer == "pm" and hour != 12:
        hour += 12
    elif mer == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"{tok!r} is not a valid time")
    return hour * 60 + minute


def slot_start_minutes(label: str) -> int:
    return parse_time_token(label.split("-")[0])


def short_label(label: str) -> str:
    """'10am-12pm' -> '10-12' for button text."""
    parts = label.split("-")
    if len(parts) != 2:
        return label
    return f"{parts[0].replace('am', '').replace('pm', '')}-{parts[1].replace('am', '').replace('pm', '')}"


def quarter_week(d: date) -> str:
    return f"Q{(d.month - 1) // 3 + 1} W{d.isocalendar().week + WEEK_NUM_OFFSET}"


def fmt_day(d: date) -> str:
    return f"{d.day} {d.strftime('%B')}"


def fmt_range(a: date, b: date) -> str:
    return f"{fmt_day(a)} - {fmt_day(b)}"


def is_owner(user_id: int) -> bool:
    """Set in config, so it survives anything done in-chat. Can't be demoted."""
    return user_id in ADMIN_IDS


def is_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    if not ADMIN_IDS:
        return True          # nothing configured yet — open until an owner is set
    row = q1(
        "SELECT role FROM agents WHERE user_id=? AND active=1", (user_id,)
    )
    return bool(row and row["role"] == "admin")


def rate_for(agent_id: int, on: date) -> int:
    """Hourly rate in cents that applied on a given day."""
    row = q1(
        "SELECT cents FROM pay_rates WHERE agent_id=? AND effective_from<=? "
        "ORDER BY effective_from DESC, id DESC LIMIT 1",
        (agent_id, on.isoformat()),
    )
    if row:
        return row["cents"]
    row = q1(
        "SELECT cents FROM pay_rates WHERE agent_id IS NULL AND effective_from<=? "
        "ORDER BY effective_from DESC, id DESC LIMIT 1",
        (on.isoformat(),),
    )
    return row["cents"] if row else DEFAULT_RATE_CENTS


def money(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"


def slot_minutes(label: str) -> int:
    """How long a slot lasts, e.g. '10am-12pm' -> 120. Handles crossing midnight."""
    parts = label.split("-")
    if len(parts) != 2:
        return 0
    try:
        a, b = parse_time_token(parts[0]), parse_time_token(parts[1])
    except ValueError:
        return 0
    if b <= a:
        b += 24 * 60
    return b - a


def slot_run_from(slot_id: int):
    """A slot plus any back-to-back slots the same agent holds after it.

    Someone rostered 10-12 and 12-2 clocks in once, so the entry has to cover
    both blocks rather than stopping at the end of the first.
    """
    first = q1(
        """SELECT s.id, s.label, s.start_min, s.day_id, d.the_date
           FROM slots s JOIN days d ON d.id = s.day_id WHERE s.id=?""",
        (slot_id,),
    )
    if not first:
        return []
    holder = q1("SELECT user_id FROM signups WHERE slot_id=?", (slot_id,))
    if not holder:
        return [first]
    same_day = q(
        """SELECT s.id, s.label, s.start_min FROM slots s
           JOIN signups su ON su.slot_id = s.id
           WHERE s.day_id=? AND su.user_id=? ORDER BY s.start_min""",
        (first["day_id"], holder["user_id"]),
    )
    run_ = [first]
    cursor = first["start_min"] + slot_minutes(first["label"])
    for cand in same_day:
        if cand["id"] == first["id"]:
            continue
        if cand["start_min"] == cursor:
            run_.append(cand)
            cursor += slot_minutes(cand["label"])
    return run_


def entry_paid_minutes(row) -> int:
    """Minutes an entry is paid for."""
    if not row["clock_out"]:
        return 0
    actual = entry_minutes(row)
    if PAY_MODE != "slot" or not row["slot_id"]:
        return actual
    out = datetime.fromisoformat(row["clock_out"])
    day = date.fromisoformat(row["the_date"])
    total = 0
    for sl in slot_run_from(row["slot_id"]):
        start = sl["start_min"]
        end_min = start + slot_minutes(sl["label"])
        slot_end = datetime.combine(day, time(0, 0), TZ) + timedelta(minutes=end_min)
        if out >= slot_end - timedelta(minutes=SLOT_GRACE_MIN):
            total += slot_minutes(sl["label"])
    return total or actual


def entry_minutes(row) -> int:
    if not row["clock_out"]:
        return 0
    a = datetime.fromisoformat(row["clock_in"])
    b = datetime.fromisoformat(row["clock_out"])
    return max(0, int((b - a).total_seconds() // 60))


def month_bounds(d: date) -> tuple[date, date]:
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt - timedelta(days=1)


def timesheet(agent_id: int, first: date, last: date) -> dict:
    rows = q(
        "SELECT * FROM time_entries WHERE agent_id=? AND the_date BETWEEN ? AND ? "
        "ORDER BY the_date, clock_in",
        (agent_id, first.isoformat(), last.isoformat()),
    )
    shifts, minutes, cents, open_count = [], 0, 0, 0
    for r in rows:
        if not r["clock_out"]:
            open_count += 1
            continue
        m = entry_paid_minutes(r)
        actual = entry_minutes(r)
        day = date.fromisoformat(r["the_date"])
        pay = round(m / 60 * rate_for(agent_id, day))
        minutes += m
        cents += pay
        shifts.append({
            "row": r, "date": day, "minutes": m,
            "actual": actual, "cents": pay,
        })
    return {
        "shifts": shifts,
        "minutes": minutes,
        "cents": cents,
        "open": open_count,
        "days": len({s["date"] for s in shifts}),
    }


def hhmm(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m"


def setting(key: str, default: str = "") -> str:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row and row["value"] else default


def set_setting(key: str, value: str) -> None:
    run(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def support_for(user_id: int, fallback: str = "") -> str:
    """Who to list as Support: their own override, else the team default."""
    row = q1("SELECT support_name FROM agents WHERE user_id=?", (user_id,))
    if row and row["support_name"]:
        return row["support_name"]
    return setting("support_name", "") or fallback


def opening_block(user_id: int, name: str, slot_label: str) -> str:
    return (
        "[OPENING]\n"
        f"{name} on Livechat : {slot_label}\n"
        f"Support: {support_for(user_id, name)}"
    )


def display_name_of(user_id: int, fallback: str = "") -> str:
    """The roster name if one is set, otherwise whatever Telegram says."""
    row = q1(
        "SELECT display_name, name FROM agents WHERE user_id=?", (user_id,)
    )
    if row and row["display_name"]:
        return row["display_name"]
    if row and row["name"]:
        return row["name"]
    return fallback


def agent_status(user_id: int) -> str | None:
    """'active', 'pending', 'declined', or None if never seen."""
    if is_owner(user_id):
        return "active"
    row = q1("SELECT status FROM agents WHERE user_id=?", (user_id,))
    return row["status"] if row else None


def has_access(user_id: int) -> bool:
    return agent_status(user_id) == "active"


def admin_ids() -> list:
    """Everyone who should see access requests."""
    ids = set(ADMIN_IDS)
    ids |= {
        r["user_id"]
        for r in q(
            "SELECT user_id FROM agents WHERE role='admin' AND status='active'"
        )
    }
    return sorted(ids)


def role_of(user_id: int) -> str:
    if is_owner(user_id):
        return "owner"
    row = q1("SELECT role FROM agents WHERE user_id=? AND active=1", (user_id,))
    return row["role"] if row else "agent"


def open_week() -> sqlite3.Row | None:
    return q1("SELECT * FROM weeks WHERE status='open' ORDER BY id DESC LIMIT 1")


def latest_week() -> sqlite3.Row | None:
    return q1("SELECT * FROM weeks ORDER BY id DESC LIMIT 1")


def touch_agent(user, dm_ok: int | None = None) -> None:
    existing = q1("SELECT * FROM agents WHERE user_id=?", (user.id,))
    if existing:
        run(
            "UPDATE agents SET name=?, username=?, dm_ok=COALESCE(?, dm_ok) "
            "WHERE user_id=?",
            (user.full_name, user.username, dm_ok, user.id),
        )
    else:
        run(
            "INSERT INTO agents (user_id, name, username, dm_ok) VALUES (?,?,?,?)",
            (user.id, user.full_name, user.username, dm_ok or 0),
        )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_day(day_id: int) -> tuple[str, InlineKeyboardMarkup]:
    day = q1("SELECT * FROM days WHERE id=?", (day_id,))
    week = q1("SELECT * FROM weeks WHERE id=?", (day["week_id"],))
    slots = q("SELECT * FROM slots WHERE day_id=? ORDER BY idx", (day_id,))
    closed = week["status"] != "open"

    d = date.fromisoformat(day["the_date"])
    lines = [f"<b>{day['name']} — {fmt_day(d)}</b>"]
    buttons = []

    for s in slots:
        names = [
            r["name"]
            for r in q(
                "SELECT name FROM signups WHERE slot_id=? ORDER BY ts", (s["id"],)
            )
        ]
        filled = ", ".join(names)
        lines.append(f"{s['label']}: {esc(filled)}")
        if len(names) >= SLOT_CAPACITY:
            tag = " ✓"
        elif names:
            tag = f" ({len(names)})"
        else:
            tag = ""
        buttons.append(
            InlineKeyboardButton(
                short_label(s["label"]) + tag, callback_data=f"t:{s['id']}"
            )
        )

    if closed:
        lines.append("\n<i>Submissions closed.</i>")

    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    return "\n".join(lines), InlineKeyboardMarkup([] if closed else rows)


def week_stats(week_id: int) -> dict:
    roster = q("SELECT * FROM agents WHERE status='active' AND on_avails=1")
    confirmed = {
        r["user_id"] for r in q("SELECT user_id FROM confirmations WHERE week_id=?", (week_id,))
    }
    gaps = q(
        """SELECT d.name, d.the_date, s.label,
                  (SELECT COUNT(*) FROM signups WHERE slot_id = s.id) AS n
           FROM slots s JOIN days d ON d.id = s.day_id
           WHERE d.week_id = ?
           ORDER BY d.idx, s.idx""",
        (week_id,),
    )
    return {
        "roster": roster,
        "confirmed": confirmed,
        "missing": [a for a in roster if a["user_id"] not in confirmed],
        "gaps": [g for g in gaps if g["n"] < MIN_PER_SLOT],
        "total_slots": len(gaps),
    }


def slot_holders(slot_id: int) -> list[sqlite3.Row]:
    return q(
        "SELECT user_id, name FROM signups WHERE slot_id=? ORDER BY ts", (slot_id,)
    )


def slot_taken_by(slot_id: int, user_id: int) -> str | None:
    """Returns the blocking holder's name, or None if the user may claim it."""
    holders = slot_holders(slot_id)
    if any(h["user_id"] == user_id for h in holders):
        return None
    if len(holders) < SLOT_CAPACITY:
        return None
    return ", ".join(h["name"] for h in holders)


def is_locked(week_id: int, user_id: int) -> bool:
    """A confirmed week is locked until the agent unlocks it."""
    return bool(
        q1("SELECT 1 FROM confirmations WHERE week_id=? AND user_id=?", (week_id, user_id))
    )


def render_board(week_id: int, compact: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """The whole week in one message."""
    w = q1("SELECT * FROM weeks WHERE id=?", (week_id,))
    st = week_stats(week_id)
    a, b = date.fromisoformat(w["start_date"]), date.fromisoformat(w["end_date"])
    deadline = datetime.fromisoformat(w["deadline"])
    closed = w["status"] != "open"

    lines = [f"📋 <b>{w['label']}: {fmt_range(a, b)}</b>", ""]
    if w["key_events"]:
        lines += ["<b>Key Events</b>", w["key_events"], ""]
    lines.append(
        "🔒 <b>Submissions closed</b>"
        if closed
        else f"⏰ Confirm by <b>{deadline.strftime('%a %-d %b, %H:%M')}</b>"
    )
    lines.append(f"✅ Confirmed: {len(st['confirmed'])}/{len(st['roster'])}")
    if st["gaps"]:
        lines.append(f"⚠️ {len(st['gaps'])} slot(s) still uncovered")
    else:
        lines.append("🎉 Every slot covered")

    rows: list[list[InlineKeyboardButton]] = []

    for d in q("SELECT * FROM days WHERE week_id=? ORDER BY idx", (week_id,)):
        the_date = date.fromisoformat(d["the_date"])
        lines += ["", f"<b>{d['name']} — {fmt_day(the_date)}</b>"]
        slot_buttons = []
        for s in q("SELECT * FROM slots WHERE day_id=? ORDER BY idx", (d["id"],)):
            names = [
                r["name"]
                for r in q(
                    "SELECT name FROM signups WHERE slot_id=? ORDER BY ts", (s["id"],)
                )
            ]
            if not names:
                shown = ""
            elif compact and len(names) > 3:
                shown = ", ".join(names[:3]) + f" +{len(names) - 3}"
            else:
                shown = ", ".join(names)
            lines.append(f"{s['label']}: {esc(shown)}")
            if len(names) >= SLOT_CAPACITY:
                tag = " ✓"
            elif names:
                tag = f" ({len(names)})"
            else:
                tag = ""
            slot_buttons.append(
                InlineKeyboardButton(
                    short_label(s["label"]) + tag, callback_data=f"t:{s['id']}"
                )
            )
        if not closed and GROUP_BUTTONS:
            rows.append(
                [InlineKeyboardButton(f"— {d['name']} —", callback_data="t:noop")]
            )
            rows += [slot_buttons[i : i + 5] for i in range(0, len(slot_buttons), 5)]

    if st["missing"] and not closed:
        names = ", ".join(
            f"@{x['username']}" if x["username"] else x["name"] for x in st["missing"][:10]
        )
        extra = f" +{len(st['missing']) - 10}" if len(st["missing"]) > 10 else ""
        lines += ["", f"⏳ Not yet confirmed: {names}{extra}"]

    if not closed and GROUP_BUTTONS:
        rows.append(
            [
                InlineKeyboardButton("✅ Slots", callback_data=f"c:{week_id}"),
                InlineKeyboardButton("🔓 Slots", callback_data=f"u:{week_id}"),
            ]
        )
    elif not closed:
        lines += ["", "<i>Fill yours in by messaging me /plan</i>"]

    text = "\n".join(lines)
    # Telegram caps messages at 4096 characters. Shorten name lists if we're close.
    if len(text) > 3900 and not compact:
        return render_board(week_id, compact=True)
    return text, InlineKeyboardMarkup(rows)


async def refresh_board(context: ContextTypes.DEFAULT_TYPE, week_id: int) -> None:
    w = q1("SELECT * FROM weeks WHERE id=?", (week_id,))
    if not w or not w["header_msg_id"]:
        return
    text, kb = render_board(week_id)
    try:
        await context.bot.edit_message_text(
            chat_id=GROUP_CHAT_ID,
            message_id=w["header_msg_id"],
            text=text,
            reply_markup=kb,
            parse_mode=constants.ParseMode.HTML,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Board refresh failed: %s", e)


async def refresh_group(context: ContextTypes.DEFAULT_TYPE, week_id: int,
                        day_id: int | None = None) -> None:
    """Update whichever group layout is in use."""
    if BOARD_MODE == "single":
        await refresh_board(context, week_id)
    else:
        if day_id:
            await refresh_day(context, day_id)
        await refresh_header(context, week_id)


def render_header(week_id: int) -> tuple[str, InlineKeyboardMarkup]:
    w = q1("SELECT * FROM weeks WHERE id=?", (week_id,))
    st = week_stats(week_id)
    a, b = date.fromisoformat(w["start_date"]), date.fromisoformat(w["end_date"])
    deadline = datetime.fromisoformat(w["deadline"])

    lines = [f"📋 <b>{w['label']}: {fmt_range(a, b)}</b>", ""]
    if w["key_events"]:
        lines += ["<b>Key Events</b>", w["key_events"], ""]

    if w["status"] == "open":
        lines.append(f"⏰ Confirm by <b>{deadline.strftime('%a %-d %b, %H:%M')}</b>")
    else:
        lines.append("🔒 <b>Submissions closed</b>")

    lines.append(f"✅ Confirmed: {len(st['confirmed'])}/{len(st['roster'])}")

    if st["gaps"]:
        preview = ", ".join(
            f"{g['name']} {short_label(g['label'])}" for g in st["gaps"][:6]
        )
        more = f" +{len(st['gaps']) - 6} more" if len(st["gaps"]) > 6 else ""
        lines.append(f"⚠️ Unfilled ({len(st['gaps'])}): {preview}{more}")
    else:
        lines.append("🎉 Every slot covered")

    if st["missing"] and w["status"] == "open":
        names = ", ".join(
            f"@{a['username']}" if a["username"] else a["name"]
            for a in st["missing"][:10]
        )
        extra = f" +{len(st['missing']) - 10}" if len(st["missing"]) > 10 else ""
        lines.append(f"⏳ Not yet confirmed: {names}{extra}")

    kb = []
    if w["status"] == "open":
        kb = [[InlineKeyboardButton("✅ Slots", callback_data=f"c:{week_id}")]]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def refresh_day(context: ContextTypes.DEFAULT_TYPE, day_id: int) -> None:
    day = q1("SELECT * FROM days WHERE id=?", (day_id,))
    if not day or not day["msg_id"]:
        return
    text, kb = render_day(day_id)
    try:
        await context.bot.edit_message_text(
            chat_id=GROUP_CHAT_ID,
            message_id=day["msg_id"],
            text=text,
            reply_markup=kb,
            parse_mode=constants.ParseMode.HTML,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Day refresh failed: %s", e)


async def refresh_header(context: ContextTypes.DEFAULT_TYPE, week_id: int) -> None:
    w = q1("SELECT * FROM weeks WHERE id=?", (week_id,))
    if not w or not w["header_msg_id"]:
        return
    text, kb = render_header(week_id)
    try:
        await context.bot.edit_message_text(
            chat_id=GROUP_CHAT_ID,
            message_id=w["header_msg_id"],
            text=text,
            reply_markup=kb,
            parse_mode=constants.ParseMode.HTML,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Header refresh failed: %s", e)


# --------------------------------------------------------------------------
# /newweek conversation (runs in DM with an admin)
# --------------------------------------------------------------------------

ASK_DATE, ASK_LABEL, ASK_EVENTS, ASK_SLOTS, ASK_DEADLINE, CONFIRM = range(6)


async def newweek(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly to set up a week 🙂")
        return ConversationHandler.END
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can open a week.")
        return ConversationHandler.END
    if not GROUP_CHAT_ID:
        await update.message.reply_text(
            "GROUP_CHAT_ID isn't set yet, so I don't know where to post.\n\n"
            "Add me to your livechat group, send /chatid there, then put that number "
            "in your env config and restart me."
        )
        return ConversationHandler.END

    today = now().date()
    nxt = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    context.user_data["draft"] = {}
    await update.message.reply_text(
        "Let's set up a new week.\n\n"
        f"<b>1/5 — Which Monday does it start?</b>\nSend a date like <code>{nxt}</code>, "
        "or <code>-</code> to use that one.\n\nSend /cancel any time.",
        parse_mode=constants.ParseMode.HTML,
    )
    context.user_data["draft"]["suggested_monday"] = nxt
    return ASK_DATE


async def got_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data["draft"]
    txt = update.message.text.strip()
    if txt == "-":
        monday = draft["suggested_monday"]
    else:
        try:
            monday = date.fromisoformat(txt)
        except ValueError:
            await update.message.reply_text("Use YYYY-MM-DD, e.g. 2026-08-17")
            return ASK_DATE
    if monday.weekday() != 0:
        monday -= timedelta(days=monday.weekday())
        await update.message.reply_text(f"Rolled back to the Monday: {monday}")
    draft["monday"] = monday
    draft["sunday"] = monday + timedelta(days=6)

    await update.message.reply_text(
        f"<b>2/5 — Week label?</b>\nSend <code>-</code> for "
        f"<code>{quarter_week(monday)}</code>.",
        parse_mode=constants.ParseMode.HTML,
    )
    return ASK_LABEL


async def got_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data["draft"]
    txt = update.message.text.strip()
    draft["label"] = quarter_week(draft["monday"]) if txt == "-" else txt
    await update.message.reply_text(
        "<b>3/5 — Key Events for the week?</b>\n"
        "e.g. <code>💻 Zoom Huddle: Thur, 6pm</code>\n"
        "Multiple lines are fine. Send <code>-</code> for none.",
        parse_mode=constants.ParseMode.HTML,
    )
    return ASK_EVENTS


async def got_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data["draft"]
    txt = update.message.text.strip()
    draft["events"] = None if txt == "-" else txt
    names = [r["name"] for r in q("SELECT name FROM presets ORDER BY name")]
    await update.message.reply_text(
        "<b>4/5 — Timings.</b>\n"
        "Send <code>-</code> for your standard pattern, or a preset name.\n\n"
        f"<b>Presets:</b> <code>{'</code>, <code>'.join(names)}</code>\n"
        "<i>campaign</i> = every day 10am through 12am, for 11.11 / 12.12 weeks.\n\n"
        "Or send custom lines, leaving out any day with no shifts:\n"
        "<code>MON: 10am-12pm, 12pm-2pm, 2pm-4pm\n"
        "SAT: 10am-12pm, 8pm-10pm, 10pm-12am</code>",
        parse_mode=constants.ParseMode.HTML,
    )
    return ASK_SLOTS


async def got_slots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data["draft"]
    txt = update.message.text.strip()
    preset_names = [r["name"] for r in q("SELECT name FROM presets ORDER BY name")]

    if txt == "-":
        draft["slots"] = dict(DEFAULT_SLOTS)
    elif "\n" not in txt and ":" not in txt:
        match = next((n for n in preset_names if n.lower() == txt.lower()), None)
        if not match:
            await update.message.reply_text(
                f"No preset called {txt!r}. Available: {', '.join(preset_names)}"
            )
            return ASK_SLOTS
        draft["slots"] = json.loads(q1("SELECT config FROM presets WHERE name=?", (match,))["config"])
    else:
        parsed: dict[str, list[str]] = {}
        for raw in txt.splitlines():
            if not raw.strip():
                continue
            if ":" not in raw:
                await update.message.reply_text(
                    f"Couldn't read this line:\n<code>{raw}</code>\n"
                    "It needs to look like <code>MON: 10am-12pm, 12pm-2pm</code>",
                    parse_mode=constants.ParseMode.HTML,
                )
                return ASK_SLOTS
            day, rest = raw.split(":", 1)
            day = day.strip().upper()
            if day not in DAY_NAMES:
                await update.message.reply_text(
                    f"Unknown day {day!r}. Use: {', '.join(DAY_NAMES)}"
                )
                return ASK_SLOTS
            labels = [s.strip() for s in rest.split(",") if s.strip()]
            try:
                for lbl in labels:
                    slot_start_minutes(lbl)
            except ValueError as e:
                await update.message.reply_text(f"{e}\nTry again for that day.")
                return ASK_SLOTS
            parsed[day] = labels
        if not parsed:
            await update.message.reply_text("No days found — try again.")
            return ASK_SLOTS
        draft["slots"] = parsed

    default_deadline = datetime.combine(
        draft["monday"] - timedelta(days=1), time(23, 59), TZ
    )
    draft["default_deadline"] = default_deadline
    await update.message.reply_text(
        f"<b>5/5 — Submission deadline?</b>\nSend <code>-</code> for "
        f"<code>{default_deadline.strftime('%Y-%m-%d %H:%M')}</code> "
        f"({default_deadline.strftime('%a')}), or give your own in the same format.",
        parse_mode=constants.ParseMode.HTML,
    )
    return ASK_DEADLINE


async def got_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data["draft"]
    txt = update.message.text.strip()
    if txt == "-":
        dl = draft["default_deadline"]
    else:
        try:
            dl = datetime.strptime(txt, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        except ValueError:
            await update.message.reply_text("Use YYYY-MM-DD HH:MM, e.g. 2026-08-16 23:59")
            return ASK_DEADLINE
    draft["deadline"] = dl

    total = sum(len(v) for v in draft["slots"].values())
    preview = "\n".join(
        f"  {d}: {', '.join(draft['slots'][d])}" for d in DAY_NAMES if d in draft["slots"]
    )
    await update.message.reply_text(
        f"<b>Ready to post:</b>\n\n"
        f"{draft['label']}: {fmt_range(draft['monday'], draft['sunday'])}\n"
        f"Key Events: {draft['events'] or '—'}\n"
        f"Deadline: {dl.strftime('%a %d %b %H:%M')}\n"
        f"{total} slots across {len(draft['slots'])} days:\n{preview}\n\n"
        "Send <code>post</code> to publish it to the group, or /cancel.",
        parse_mode=constants.ParseMode.HTML,
    )
    return CONFIRM


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.strip().lower() not in {"post", "yes", "y"}:
        await update.message.reply_text("Send <code>post</code> to publish, or /cancel.",
                                        parse_mode=constants.ParseMode.HTML)
        return CONFIRM

    draft = context.user_data["draft"]
    async with write_lock:
        prev = open_week()
        if prev:
            run("UPDATE weeks SET status='closed' WHERE id=?", (prev["id"],))

        cur = run(
            "INSERT INTO weeks (label, start_date, end_date, key_events, deadline) "
            "VALUES (?,?,?,?,?)",
            (
                draft["label"],
                draft["monday"].isoformat(),
                draft["sunday"].isoformat(),
                draft["events"],
                draft["deadline"].isoformat(),
            ),
        )
        week_id = cur.lastrowid

        for i, name in enumerate(DAY_NAMES):
            if name not in draft["slots"]:
                continue
            the_date = draft["monday"] + timedelta(days=i)
            dcur = run(
                "INSERT INTO days (week_id, idx, name, the_date) VALUES (?,?,?,?)",
                (week_id, i, name, the_date.isoformat()),
            )
            day_id = dcur.lastrowid
            for j, lbl in enumerate(draft["slots"][name]):
                run(
                    "INSERT INTO slots (day_id, idx, label, start_min) VALUES (?,?,?,?)",
                    (day_id, j, lbl, slot_start_minutes(lbl)),
                )

    bot = context.bot
    if BOARD_MODE == "single":
        text, kb = render_board(week_id)
    else:
        text, kb = render_header(week_id)
    header = await send_group(
        bot, text, reply_markup=kb, parse_mode=constants.ParseMode.HTML
    )
    run("UPDATE weeks SET header_msg_id=? WHERE id=?", (header.message_id, week_id))
    try:
        await bot.pin_chat_message(GROUP_CHAT_ID, header.message_id, disable_notification=True)
    except (BadRequest, Forbidden):
        log.info("Couldn't pin — bot may not be a group admin.")

    if BOARD_MODE != "single":
        for d in q("SELECT * FROM days WHERE week_id=? ORDER BY idx", (week_id,)):
            day_text, day_kb = render_day(d["id"])
            msg = await send_group(
                bot, day_text, reply_markup=day_kb,
                parse_mode=constants.ParseMode.HTML,
            )
            run("UPDATE days SET msg_id=? WHERE id=?", (msg.message_id, d["id"]))

    if POST_INTRO:
        if GROUP_BUTTONS:
            intro = [
                "☝️ Tap the buttons above to claim your slots, "
                "or DM me <code>/plan</code> to do the whole week at once.",
                "",
                "Finish with <b>✅ Slots</b> on the pinned message.",
            ]
        else:
            intro = [
                "☝️ This board updates itself. To pick your slots, "
                "DM me <code>/plan</code>.",
            ]
        await send_group(
            bot, "\n".join(intro), parse_mode=constants.ParseMode.HTML
        )

    schedule_week_jobs(context.application, week_id)
    await update.message.reply_text("Posted to the group ✅")
    context.user_data.pop("draft", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("draft", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Claiming slots
# --------------------------------------------------------------------------


async def on_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await gate_cb(query):
        return
    raw = query.data.split(":", 1)[1]
    if raw == "noop":
        await query.answer()
        return

    slot_id = int(raw)
    user = query.from_user

    row = q1(
        """SELECT s.id, s.label, d.id AS day_id, d.name AS day_name,
                  w.id AS week_id, w.status
           FROM slots s JOIN days d ON d.id = s.day_id
           JOIN weeks w ON w.id = d.week_id WHERE s.id=?""",
        (slot_id,),
    )
    if not row:
        await query.answer("That slot no longer exists.", show_alert=True)
        return
    if row["status"] != "open":
        await query.answer("Submissions for this week are closed.", show_alert=True)
        return
    if is_locked(row["week_id"], user.id):
        await query.answer(
            "🔒 Your week is confirmed, so your slots are locked.\n\n"
            "Tap 🔓 Slots to make changes.",
            show_alert=True,
        )
        return

    async with write_lock:
        touch_agent(user)
        existing = q1(
            "SELECT 1 FROM signups WHERE slot_id=? AND user_id=?", (slot_id, user.id)
        )
        if not existing:
            holder = slot_taken_by(slot_id, user.id)
            if holder:
                await query.answer(
                    f"{row['day_name']} {row['label']} is already taken by {holder}.",
                    show_alert=True,
                )
                return
        if existing:
            run("DELETE FROM signups WHERE slot_id=? AND user_id=?", (slot_id, user.id))
            note = f"Dropped {row['day_name']} {row['label']}"
        else:
            run(
                "INSERT INTO signups (slot_id, user_id, name, ts) VALUES (?,?,?,?)",
                (slot_id, user.id, display_name_of(user.id, user.full_name),
                 now().isoformat()),
            )
            note = f"✅ {row['day_name']} {row['label']}"

    await query.answer(note)
    await refresh_group(context, row["week_id"], row["day_id"])


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await gate_cb(query):
        return
    kind, raw = query.data.split(":", 1)
    week_id = int(raw)
    user = query.from_user
    w = q1("SELECT * FROM weeks WHERE id=?", (week_id,))
    if not w or w["status"] != "open":
        await query.answer("This week is already closed.", show_alert=True)
        return

    async with write_lock:
        touch_agent(user)
        already = is_locked(week_id, user.id)

        if kind == "u":
            if not already:
                await query.answer(
                    "Your slots aren't locked — you can tap them to change them.",
                    show_alert=True,
                )
                return
            run(
                "DELETE FROM confirmations WHERE week_id=? AND user_id=?",
                (week_id, user.id),
            )
            note = "🔓 Unlocked. Change your slots, then confirm again."
        else:
            if already:
                await query.answer(
                    "You've already confirmed. Tap 🔓 Slots to change anything.",
                    show_alert=True,
                )
                return
            n = q1(
                """SELECT COUNT(*) AS n FROM signups su
                   JOIN slots s ON s.id = su.slot_id JOIN days d ON d.id = s.day_id
                   WHERE d.week_id=? AND su.user_id=?""",
                (week_id, user.id),
            )["n"]
            run(
                "INSERT INTO confirmations (week_id, user_id, ts) VALUES (?,?,?)",
                (week_id, user.id, now().isoformat()),
            )
            note = (
                f"✅ Confirmed — {n} slot(s), now locked."
                if n
                else "✅ Confirmed as unavailable this week (0 slots)."
            )

    await query.answer(note, show_alert=True)
    await refresh_group(context, week_id)


def _my_slots(user_id: int, week_id: int) -> set:
    return {
        r["slot_id"]
        for r in q(
            """SELECT su.slot_id FROM signups su
               JOIN slots s ON s.id = su.slot_id JOIN days d ON d.id = s.day_id
               WHERE d.week_id=? AND su.user_id=?""",
            (week_id, user_id),
        )
    }


def _plan_footer(user_id: int, week_id: int, total: int) -> list:
    confirmed = is_locked(week_id, user_id)
    rows = []
    if not confirmed:
        rows.append(
            [
                InlineKeyboardButton("📋 Same as last week", callback_data="pq:last"),
                InlineKeyboardButton("🗑 Clear all", callback_data="pq:clear"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "🔓 Slots" if confirmed else "✅ Slots", callback_data="pq:confirm"
            )
        ]
    )
    return rows


def render_plan(user_id: int, week_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Overview: one button per day, showing how many slots you hold."""
    w = q1("SELECT * FROM weeks WHERE id=?", (week_id,))
    mine = _my_slots(user_id, week_id)
    confirmed = is_locked(week_id, user_id)

    lines = [f"🗓 <b>Your week — {w['label']}</b>", ""]
    rows = []
    for d in q("SELECT * FROM days WHERE week_id=? ORDER BY idx", (week_id,)):
        slots = q("SELECT * FROM slots WHERE day_id=? ORDER BY idx", (d["id"],))
        picked = [s["label"] for s in slots if s["id"] in mine]
        free = sum(
            1 for s in slots
            if s["id"] not in mine and len(slot_holders(s["id"])) < SLOT_CAPACITY
        )
        lines.append(
            f"<b>{d['name']}</b>: {', '.join(picked) if picked else '—'}"
        )
        if picked and free:
            label = f"{d['name']}  ✅ {len(picked)} yours · {free} available"
        elif picked:
            label = f"{d['name']}  ✅ {len(picked)} yours · none left"
        elif free:
            label = f"{d['name']}  ·  {free} available"
        else:
            label = f"{d['name']}  ·  none left"
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"pd:{d['id']}")]
        )

    lines += [
        "",
        f"<b>{len(mine)} slot(s) selected.</b>",
        "🔒 Confirmed and locked. Unlock below to change anything."
        if confirmed
        else "Tap a day to pick your times.",
    ]
    rows += _plan_footer(user_id, week_id, len(mine))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def render_plan_day(user_id: int, day_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """One day at a time — far less to read than the whole week."""
    d = q1("SELECT * FROM days WHERE id=?", (day_id,))
    week_id = d["week_id"]
    mine = _my_slots(user_id, week_id)
    confirmed = is_locked(week_id, user_id)
    the_date = date.fromisoformat(d["the_date"])

    lines = [f"🗓 <b>{d['name']} — {fmt_day(the_date)}</b>", ""]
    rows = []
    for s in q("SELECT * FROM slots WHERE day_id=? ORDER BY idx", (day_id,)):
        holders = slot_holders(s["id"])
        if s["id"] in mine:
            state, mark, btn = "claimed by you", "✅", "✅ {} — yours"
        elif len(holders) >= SLOT_CAPACITY:
            state = "taken by " + esc(", ".join(h["name"] for h in holders))
            mark, btn = "🔒", "🔒 {} — taken"
        else:
            state, mark, btn = "available", "▫️", "▫️ {} — available"
        lines.append(f"{mark} {s['label']} — {state}")
        rows.append(
            [InlineKeyboardButton(btn.format(s["label"]), callback_data=f"p:{s['id']}")]
        )

    lines += [
        "",
        "🔒 Locked — unlock to change." if confirmed else "Tap a time to take or drop it.",
    ]
    rows.append([InlineKeyboardButton("← All days", callback_data="pd:list")])
    rows += _plan_footer(user_id, week_id, len(mine))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("DM me /plan to fill in your whole week at once 🙂")
        return
    if not await gate(update):
        return
    w = open_week()
    if not w:
        await update.message.reply_text("No week is open right now.")
        return
    touch_agent(update.effective_user, dm_ok=1)
    text, kb = render_plan(update.effective_user.id, w["id"])
    await update.message.reply_text(
        text, reply_markup=kb, parse_mode=constants.ParseMode.HTML
    )


async def refresh_plan(query, user_id: int, week_id: int, day_id: int | None = None) -> None:
    if day_id:
        text, kb = render_plan_day(user_id, day_id)
    else:
        text, kb = render_plan(user_id, week_id)
    try:
        await query.edit_message_text(
            text, reply_markup=kb, parse_mode=constants.ParseMode.HTML
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Plan refresh failed: %s", e)


async def on_plan_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await gate_cb(query):
        return
    raw = query.data.split(":", 1)[1]
    if raw == "noop":
        await query.answer()
        return

    slot_id = int(raw)
    user = query.from_user
    row = q1(
        """SELECT s.id, s.label, d.id AS day_id, d.name AS day_name,
                  w.id AS week_id, w.status
           FROM slots s JOIN days d ON d.id = s.day_id
           JOIN weeks w ON w.id = d.week_id WHERE s.id=?""",
        (slot_id,),
    )
    if not row or row["status"] != "open":
        await query.answer("This week is closed.", show_alert=True)
        return
    if is_locked(row["week_id"], user.id):
        await query.answer(
            "🔒 Your week is confirmed. Tap 🔓 Slots to change it.",
            show_alert=True,
        )
        return

    async with write_lock:
        touch_agent(user, dm_ok=1)
        mine = q1(
            "SELECT 1 FROM signups WHERE slot_id=? AND user_id=?", (slot_id, user.id)
        )
        if not mine:
            holder = slot_taken_by(slot_id, user.id)
            if holder:
                await query.answer(
                    f"{row['day_name']} {row['label']} is already taken by {holder}.",
                    show_alert=True,
                )
                return
        if mine:
            run("DELETE FROM signups WHERE slot_id=? AND user_id=?", (slot_id, user.id))
            note = f"Dropped {row['day_name']} {row['label']}"
        else:
            run(
                "INSERT INTO signups (slot_id, user_id, name, ts) VALUES (?,?,?,?)",
                (slot_id, user.id, display_name_of(user.id, user.full_name),
                 now().isoformat()),
            )
            note = f"✅ {row['day_name']} {row['label']}"

    await query.answer(note)
    await refresh_plan(query, user.id, row["week_id"], row["day_id"])
    await refresh_group(context, row["week_id"], row["day_id"])


async def on_plan_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Move between the day list and a single day."""
    query = update.callback_query
    if not await gate_cb(query):
        return
    raw = query.data.split(":", 1)[1]
    user = query.from_user
    w = open_week()
    if not w:
        await query.answer("No week is open.", show_alert=True)
        return
    await query.answer()
    if raw == "list":
        text, kb = render_plan(user.id, w["id"])
    else:
        text, kb = render_plan_day(user.id, int(raw))
    try:
        await query.edit_message_text(
            text, reply_markup=kb, parse_mode=constants.ParseMode.HTML
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Plan nav failed: %s", e)


async def on_plan_quick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await gate_cb(query):
        return
    action = query.data.split(":", 1)[1]
    user = query.from_user
    w = open_week()
    if not w:
        await query.answer("No week is open.", show_alert=True)
        return
    week_id = w["id"]

    if action in {"clear", "last"} and is_locked(week_id, user.id):
        await query.answer(
            "🔒 Your week is confirmed. Tap 🔓 Slots first.",
            show_alert=True,
        )
        return

    async with write_lock:
        touch_agent(user, dm_ok=1)

        if action == "clear":
            run(
                """DELETE FROM signups WHERE user_id=? AND slot_id IN
                   (SELECT s.id FROM slots s JOIN days d ON d.id=s.day_id WHERE d.week_id=?)""",
                (user.id, week_id),
            )
            note = "Cleared your whole week."

        elif action == "last":
            prev = q1(
                "SELECT * FROM weeks WHERE id < ? ORDER BY id DESC LIMIT 1", (week_id,)
            )
            if not prev:
                await query.answer("No previous week to copy from.", show_alert=True)
                return
            previous = q(
                """SELECT d.name AS day_name, s.label FROM signups su
                   JOIN slots s ON s.id = su.slot_id JOIN days d ON d.id = s.day_id
                   WHERE d.week_id=? AND su.user_id=?""",
                (prev["id"], user.id),
            )
            if not previous:
                await query.answer(
                    "You had nothing booked last week — nothing to copy.", show_alert=True
                )
                return
            copied, skipped, taken = 0, 0, 0
            for p in previous:
                target = q1(
                    """SELECT s.id FROM slots s JOIN days d ON d.id = s.day_id
                       WHERE d.week_id=? AND d.name=? AND s.label=?""",
                    (week_id, p["day_name"], p["label"]),
                )
                if not target:
                    skipped += 1
                    continue
                if slot_taken_by(target["id"], user.id):
                    taken += 1
                    continue
                cur = run(
                    "INSERT OR IGNORE INTO signups (slot_id, user_id, name, ts) VALUES (?,?,?,?)",
                    (target["id"], user.id,
                     display_name_of(user.id, user.full_name), now().isoformat()),
                )
                copied += cur.rowcount
            note = f"Copied {copied} slot(s) from {prev['label']}."
            if skipped:
                note += f" {skipped} not in this week's timings."
            if taken:
                note += f" {taken} already taken by someone else."
            note += " Now adjust whatever's changed."

        elif action == "confirm":
            if is_locked(week_id, user.id):
                run(
                    "DELETE FROM confirmations WHERE week_id=? AND user_id=?",
                    (week_id, user.id),
                )
                note = "🔓 Unlocked. Change your slots, then confirm again."
            else:
                run(
                    "INSERT INTO confirmations (week_id, user_id, ts) VALUES (?,?,?)",
                    (week_id, user.id, now().isoformat()),
                )
                note = "✅ Confirmed and locked. Thanks!"
        else:
            await query.answer()
            return

    await query.answer(note, show_alert=(action in {"last", "confirm"}))
    await refresh_plan(query, user.id, week_id)
    if BOARD_MODE == "single":
        await refresh_board(context, week_id)
    else:
        if action in {"clear", "last"}:
            for d in q("SELECT id FROM days WHERE week_id=? ORDER BY idx", (week_id,)):
                await refresh_day(context, d["id"])
        await refresh_header(context, week_id)


async def cmd_shiftcall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post the on-duty tag list now, instead of waiting for the scheduled time."""
    if not is_admin(update.effective_user.id):
        return
    target = None
    if context.args:
        try:
            target = date.fromisoformat(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /shiftcall or /shiftcall 2026-08-25")
            return
    text, reason = build_shift_call(target)
    if not text:
        await update.message.reply_text(f"Nothing to post — {reason}")
        return
    await send_group(
        context.bot, text, thread=SHIFTCALL_THREAD_ID,
        parse_mode=constants.ParseMode.HTML,
    )
    await update.message.reply_text("Posted to the group ✅")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Setup helper — reports the IDs you need for the env file."""
    chat = update.effective_chat
    user = update.effective_user
    lines = [
        "<b>IDs for your config</b>",
        "",
        f"This chat ({chat.type}):",
        f"<code>{chat.id}</code>",
        "",
        f"You ({user.full_name}):",
        f"<code>{user.id}</code>",
    ]
    thread = getattr(update.message, "message_thread_id", None)
    if chat.type in (constants.ChatType.GROUP, constants.ChatType.SUPERGROUP):
        lines += ["", f"→ <code>GROUP_CHAT_ID={chat.id}</code>"]
        if thread:
            lines += [
                f"→ <code>GROUP_THREAD_ID={thread}</code>",
                "<i>(this topic — set that to post here)</i>",
            ]
        else:
            lines.append("<i>Not in a topic — posts go to General.</i>")
        if GROUP_CHAT_ID and chat.id != GROUP_CHAT_ID:
            lines.append("⚠️ This differs from the GROUP_CHAT_ID currently configured.")
        elif not GROUP_CHAT_ID:
            lines.append("Set that in your env and restart the bot.")
    else:
        lines += ["", f"→ <code>ADMIN_IDS={user.id}</code>", "",
                  "<i>Run this inside your livechat group to get GROUP_CHAT_ID.</i>"]
    await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)


async def cmd_presets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    rows = q("SELECT * FROM presets ORDER BY name")
    lines = ["<b>Saved timing presets</b>"]
    for r in rows:
        cfg = json.loads(r["config"])
        total = sum(len(v) for v in cfg.values())
        lines.append(f"\n<b>{r['name']}</b> — {total} slots / {len(cfg)} days")
        for d in DAY_NAMES:
            if d in cfg:
                lines.append(f"  {d}: {', '.join(cfg[d])}")
    lines.append(
        "\nSave the current week's timings as a preset with "
        "<code>/savepreset name</code>"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)


async def cmd_savepreset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /savepreset 11.11")
        return
    name = " ".join(context.args).strip()
    w = latest_week()
    if not w:
        await update.message.reply_text("No week to save from yet.")
        return
    cfg: dict[str, list[str]] = {}
    for d in q("SELECT * FROM days WHERE week_id=? ORDER BY idx", (w["id"],)):
        cfg[d["name"]] = [
            s["label"] for s in q("SELECT label FROM slots WHERE day_id=? ORDER BY idx", (d["id"],))
        ]
    run(
        "INSERT INTO presets (name, config) VALUES (?,?) "
        "ON CONFLICT(name) DO UPDATE SET config=excluded.config",
        (name, json.dumps(cfg)),
    )
    total = sum(len(v) for v in cfg.values())
    await update.message.reply_text(
        f"Saved preset <b>{name}</b> ({total} slots). Use it by sending "
        f"<code>{name}</code> at the timings step of /newweek.",
        parse_mode=constants.ParseMode.HTML,
    )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


ASK_NAME = 100


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return ConversationHandler.END

    status = agent_status(user.id)

    if status == "active":
        touch_agent(user, dm_ok=1)
        await update.message.reply_text(
            "👋 <b>You're in!</b>\n\n"
            "/plan — pick your slots for the week\n"
            "/clockin — start your shift\n"
            "/clockout — end your shift\n"
            "/myshifts — what you're signed up for\n"
            "/mytime — your hours this month\n"
            "/help — this list again\n\n"
            "<i>Slots are one person, first come first served.\n"
            "The group schedule updates itself — no need to post avails there.</i>",
            parse_mode=constants.ParseMode.HTML,
        )
        return ConversationHandler.END

    if status == "pending":
        await update.message.reply_text(
            "Your request is still waiting for approval. "
            "I'll message you the moment it's approved 👍"
        )
        return ConversationHandler.END

    if status == "declined":
        await update.message.reply_text(
            "Your access request wasn't approved. "
            "Please speak to your manager if you think that's a mistake."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Hello! Before I can let you in, what's your full name?\n\n"
        "<i>Use the name your manager knows you by — it's what shows on the "
        "schedule, so everyone can tell who's on which shift.</i>",
        parse_mode=constants.ParseMode.HTML,
    )
    return ASK_NAME


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    given = " ".join(update.message.text.split()).strip()

    if len(given) < 2 or len(given) > 40:
        await update.message.reply_text(
            "That doesn't look like a name — try again with 2 to 40 characters."
        )
        return ASK_NAME

    async with write_lock:
        run(
            "INSERT OR REPLACE INTO agents "
            "(user_id, name, username, dm_ok, active, role, status, "
            " display_name, requested_at) "
            "VALUES (?,?,?,1,1,'agent','pending',?,?)",
            (user.id, user.full_name, user.username, given, now().isoformat()),
        )

    await update.message.reply_text(
        f"Thanks {esc(given)}! I've sent your request to the team admins.\n\n"
        "You'll get a message here once you're approved.",
        parse_mode=constants.ParseMode.HTML,
    )

    handle = f"@{user.username}" if user.username else "no username"
    mismatch = ""
    if given.lower() != (user.full_name or "").lower():
        mismatch = f"\nTelegram name: <i>{esc(user.full_name)}</i>"
    text = (
        "🔔 <b>Access request</b>\n\n"
        f"Says they are: <b>{esc(given)}</b>{mismatch}\n"
        f"{handle} · <code>{user.id}</code>\n\n"
        "Approve only if you recognise this person.\n"
        f"<i>Wrong name? Fix it with /rename {user.id} Correct Name</i>"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"ap:{user.id}"),
                InlineKeyboardButton("🚫 Decline", callback_data=f"dn:{user.id}"),
            ]
        ]
    )
    for aid in admin_ids():
        try:
            await context.bot.send_message(
                aid, text, reply_markup=kb, parse_mode=constants.ParseMode.HTML
            )
        except Exception as e:
            log.info("Couldn't notify admin %s: %s", aid, e)
    return ConversationHandler.END


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the name that appears on the schedule."""
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /rename @handle Jia En\n"
            "or:    /rename 123456789 Jia En"
        )
        return

    target, new_name = context.args[0], " ".join(context.args[1:]).strip()
    row = None
    if target.startswith("@"):
        row = q1("SELECT * FROM agents WHERE lower(username)=lower(?)", (target[1:],))
    elif target.isdigit():
        row = q1("SELECT * FROM agents WHERE user_id=?", (int(target),))
    if not row:
        await update.message.reply_text("No one matches that. Check /roster.")
        return

    async with write_lock:
        run(
            "UPDATE agents SET display_name=? WHERE user_id=?",
            (new_name, row["user_id"]),
        )
        # keep the schedule in step with the new name
        run("UPDATE signups SET name=? WHERE user_id=?", (new_name, row["user_id"]))

    await update.message.reply_text(
        f"Renamed to <b>{esc(new_name)}</b> on the schedule.",
        parse_mode=constants.ParseMode.HTML,
    )
    w = open_week()
    if w:
        await refresh_group(context, w["id"])


async def on_access_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    kind, raw = query.data.split(":", 1)
    target_id = int(raw)
    decider = query.from_user

    if not is_admin(decider.id):
        await query.answer("Only admins can approve access.", show_alert=True)
        return

    row = q1("SELECT * FROM agents WHERE user_id=?", (target_id,))
    if not row:
        await query.answer("That request no longer exists.", show_alert=True)
        return
    if row["status"] != "pending":
        await query.answer(
            f"Already handled — {esc(row['name'])} is {row['status']}.", show_alert=True
        )
        return

    approve = kind == "ap"
    new_status = "active" if approve else "declined"
    async with write_lock:
        run(
            "UPDATE agents SET status=?, decided_by=?, decided_at=? WHERE user_id=?",
            (new_status, decider.id, now().isoformat(), target_id),
        )

    shown = row["display_name"] or row["name"]
    verdict = "approved ✅" if approve else "declined 🚫"
    await query.answer(f"{shown} {verdict}")
    try:
        await query.edit_message_text(
            f"🔔 <b>Access request</b>\n\n<b>{esc(shown)}</b>\n\n"
            f"{verdict} by {esc(decider.full_name)}",
            parse_mode=constants.ParseMode.HTML,
        )
    except BadRequest:
        pass

    try:
        if approve:
            await context.bot.send_message(
                target_id,
                "👋 <b>You're in!</b>\n\n"
            "/plan — pick your slots for the week\n"
            "/clockin — start your shift\n"
            "/clockout — end your shift\n"
            "/myshifts — what you're signed up for\n"
            "/mytime — your hours this month\n"
            "/help — this list again\n\n"
            "<i>Slots are one person, first come first served.\n"
            "The group schedule updates itself — no need to post avails there.</i>",
                parse_mode=constants.ParseMode.HTML,
            )
            await refresh_menu_for(context.bot, target_id)
        else:
            await context.bot.send_message(
                target_id,
                "Your access request wasn't approved. "
                "Please speak to your manager if you think that's a mistake.",
            )
    except Exception:
        pass


async def gate(update: Update) -> bool:
    """True if this person may use the bot. Replies if not."""
    uid = update.effective_user.id
    status = agent_status(uid)
    if status == "active":
        return True
    msg = {
        "pending": "Your request is still waiting for approval 👍",
        "declined": "You don't have access. Please speak to your manager.",
    }.get(status, "Send /start to request access.")
    if update.message:
        await update.message.reply_text(msg)
    return False


async def gate_cb(query) -> bool:
    """Same, for button taps."""
    status = agent_status(query.from_user.id)
    if status == "active":
        return True
    msg = {
        "pending": "Your access request is still pending approval.",
        "declined": "You don't have access to this.",
    }.get(status, "Send /start to the bot to request access.")
    await query.answer(msg, show_alert=True)
    return False


def current_slot_for(agent_id: int, when: datetime):
    """The rostered slot this clock-in most likely belongs to."""
    today = when.date()
    mins = when.hour * 60 + when.minute
    rows = q(
        """SELECT s.id, s.label, s.start_min, d.name AS day_name, d.the_date
           FROM signups su JOIN slots s ON s.id = su.slot_id
           JOIN days d ON d.id = s.day_id
           WHERE su.user_id=? AND d.the_date=? ORDER BY s.start_min""",
        (agent_id, today.isoformat()),
    )
    if not rows:
        return None
    # closest slot start within a couple of hours either side
    best, gap = None, 10**9
    for r in rows:
        delta = abs(r["start_min"] - mins)
        if delta < gap:
            best, gap = r, delta
    return best if gap <= 150 else None


async def cmd_clockin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly to clock in 🙂")
        return
    if not await gate(update):
        return
    user = update.effective_user
    when = now()

    open_row = q1(
        "SELECT * FROM time_entries WHERE agent_id=? AND clock_out IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (user.id,),
    )
    if open_row:
        started = datetime.fromisoformat(open_row["clock_in"])
        await update.message.reply_text(
            f"You're already clocked in since {started.strftime('%-d %b, %H:%M')}.\n"
            "Send /clockout when you finish."
        )
        return

    override = " ".join(context.args).strip() if context.args else ""
    if override:
        # Covering a shift, or clocking in for a block they aren't rostered on.
        slot = q1(
            """SELECT s.id, s.label, s.start_min, d.name AS day_name
               FROM slots s JOIN days d ON d.id = s.day_id
               WHERE d.the_date=? AND lower(s.label)=lower(?)""",
            (when.date().isoformat(), override),
        )
        slot_text = slot["label"] if slot else override
    else:
        slot = current_slot_for(user.id, when)
        slot_text = slot["label"] if slot else "—"

    async with write_lock:
        run(
            "INSERT INTO time_entries (agent_id, slot_id, the_date, clock_in, status) "
            "VALUES (?,?,?,?,'open')",
            (user.id, slot["id"] if slot else None,
             when.date().isoformat(), when.isoformat()),
        )

    nice_name = display_name_of(user.id, user.full_name)
    block = opening_block(user.id, nice_name, slot_text)

    posted = await post_ops(context.bot, block)
    if not posted:
        # No ops group configured, so give them the text to paste themselves.
        await update.message.reply_text(block, parse_mode=None)

    if slot:
        note = (
            f"⏱ Clocked in at <b>{when.strftime('%H:%M')}</b>\n"
            f"Shift: {slot['day_name']} {slot['label']}\n"
            f"Support: {esc(support_for(user.id, nice_name))}\n\n"
        )
        note += (
            "Your [OPENING] has been posted ✅\n"
            if posted else "Tap the message above to copy it.\n"
        )
        note += "Send /clockout when you finish."
        await update.message.reply_text(note, parse_mode=constants.ParseMode.HTML)
    elif override:
        note = (
            f"⏱ Clocked in at <b>{when.strftime('%H:%M')}</b>\n"
            f"Shift: {esc(override)}\n"
            f"Support: {esc(support_for(user.id, nice_name))}\n\n"
            "⚠️ That block isn't on today's roster, so it's logged as unrostered "
            "and your manager will see it flagged.\n\n"
        )
        note += (
            "Your [OPENING] has been posted ✅\n"
            if posted else "Tap the message above to copy it.\n"
        )
        note += "Send /clockout when you finish."
        await update.message.reply_text(note, parse_mode=constants.ParseMode.HTML)
    else:
        await update.message.reply_text(
            f"⏱ Clocked in at <b>{when.strftime('%H:%M')}</b>\n\n"
            "⚠️ I couldn't find a shift for you around now, so this is logged as "
            "unrostered. If you're covering a specific block, clock out and use "
            "<code>/clockin 6pm-8pm</code>.\n\n"
            "Send /clockout when you finish.",
            parse_mode=constants.ParseMode.HTML,
        )


async def cmd_clockout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly to clock out 🙂")
        return
    if not await gate(update):
        return
    user = update.effective_user
    when = now()

    open_row = q1(
        "SELECT * FROM time_entries WHERE agent_id=? AND clock_out IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (user.id,),
    )
    if not open_row:
        await update.message.reply_text(
            "You're not clocked in. Send /clockin when you start a shift."
        )
        return

    async with write_lock:
        run(
            "UPDATE time_entries SET clock_out=?, status='closed' WHERE id=?",
            (when.isoformat(), open_row["id"]),
        )
    fresh = q1("SELECT * FROM time_entries WHERE id=?", (open_row["id"],))
    mins = entry_paid_minutes(fresh)
    actual = entry_minutes(fresh)
    day = date.fromisoformat(fresh["the_date"])
    pay = round(mins / 60 * rate_for(user.id, day))

    msg = [f"✅ Clocked out at <b>{when.strftime('%H:%M')}</b>"]
    if PAY_MODE == "slot" and fresh["slot_id"] and mins != actual:
        msg.append(f"On duty: {hhmm(actual)}")
        msg.append(f"Credited: <b>{hhmm(mins)}</b> (full shift)")
    else:
        msg.append(f"Worked: <b>{hhmm(mins)}</b>")
    if pay:
        msg.append(f"Approx: {money(pay)}")
    msg.append("\n/mytime — your hours this month")
    await update.message.reply_text(
        "\n".join(msg), parse_mode=constants.ParseMode.HTML
    )


async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/support           — show who you list as support
       /support Zoe       — set it for yourself
       /support team Zoe  — admins: set the default for everyone"""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    if not await gate(update):
        return
    user = update.effective_user
    args = context.args

    if args and args[0].lower() == "team":
        if not is_admin(user.id):
            await update.message.reply_text("Only admins can set the team default.")
            return
        if len(args) < 2:
            await update.message.reply_text("Usage: /support team Zoe")
            return
        value = " ".join(args[1:]).strip()
        async with write_lock:
            set_setting("support_name", value)
        await update.message.reply_text(
            f"Team default support is now <b>{esc(value)}</b>.\n"
            "<i>Anyone who's set their own keeps theirs.</i>",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    if not args:
        mine = q1("SELECT support_name FROM agents WHERE user_id=?", (user.id,))
        own = mine["support_name"] if mine else None
        team = setting("support_name", "")
        lines = [
            f"Your openings list: <b>{esc(support_for(user.id, display_name_of(user.id, user.full_name)))}</b>"
        ]
        lines.append(
            "<i>(your own setting)</i>" if own
            else ("<i>(team default)</i>" if team else "<i>(defaults to your own name)</i>")
        )
        lines.append("\nChange it with <code>/support Zoe</code>")
        if is_admin(user.id):
            lines.append("Set it for everyone with <code>/support team Zoe</code>")
        lines.append("Clear yours with <code>/support reset</code>")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    if args[0].lower() == "reset":
        async with write_lock:
            run("UPDATE agents SET support_name=NULL WHERE user_id=?", (user.id,))
        await update.message.reply_text("Cleared — you'll use the team default.")
        return

    value = " ".join(args).strip()
    async with write_lock:
        run("UPDATE agents SET support_name=? WHERE user_id=?", (value, user.id))
    await update.message.reply_text(
        f"Your openings will list <b>{esc(value)}</b> as support.",
        parse_mode=constants.ParseMode.HTML,
    )


async def cmd_mytime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    if not await gate(update):
        return
    user = update.effective_user
    first, last = month_bounds(now().date())
    t = timesheet(user.id, first, last)

    lines = [f"🕐 <b>Your hours — {first.strftime('%B %Y')}</b>", ""]
    if not t["shifts"] and not t["open"]:
        lines.append("Nothing logged yet this month.")
    for sh in t["shifts"]:
        r = sh["row"]
        a = datetime.fromisoformat(r["clock_in"]).strftime("%H:%M")
        b = datetime.fromisoformat(r["clock_out"]).strftime("%H:%M")
        flag = " ⚠️" if r["status"] == "auto" else ""
        lines.append(
            f"{sh['date'].strftime('%a %-d %b')}  {a}–{b}  "
            f"<b>{hhmm(sh['minutes'])}</b>{flag}  <code>#{r['id']}</code>"
        )
    if t["shifts"]:
        lines += [
            "",
            f"<b>{t['days']} day(s) · {hhmm(t['minutes'])}</b>",
        ]
        if t["cents"]:
            lines.append(f"<b>Estimated: {money(t['cents'])}</b>")
            if PAY_MODE == "slot":
                lines.append(
                    "<i>Paid by rostered shift, so a full block counts even if "
                    "you clocked in a minute late.</i>"
                )
            lines.append(
                "<i>An estimate to help you plan. Your actual pay comes from HR "
                "and may differ.</i>"
            )
    if t["open"]:
        lines.append("\n⏱ You have a shift still clocked in.")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=constants.ParseMode.HTML
    )


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    rows = q(
        "SELECT * FROM agents WHERE status='pending' ORDER BY requested_at"
    )
    if not rows:
        await update.message.reply_text("No one is waiting for approval.")
        return
    await update.message.reply_text(f"{len(rows)} waiting for approval:")
    for r in rows:
        handle = f"@{r['username']}" if r["username"] else "no username"
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"ap:{r['user_id']}"),
                    InlineKeyboardButton("🚫 Decline", callback_data=f"dn:{r['user_id']}"),
                ]
            ]
        )
        await update.message.reply_text(
            f"<b>{esc(r['display_name'] or r['name'])}</b>\n"
            f"{handle} · <code>{r['user_id']}</code>",
            reply_markup=kb,
            parse_mode=constants.ParseMode.HTML,
        )


async def cmd_myshifts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly for that 🙂")
        return
    if not await gate(update):
        return
    w = latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return
    rows = q(
        """SELECT d.name, d.the_date, s.label FROM signups su
           JOIN slots s ON s.id = su.slot_id JOIN days d ON d.id = s.day_id
           WHERE d.week_id=? AND su.user_id=? ORDER BY d.idx, s.idx""",
        (w["id"], update.effective_user.id),
    )
    if not rows:
        await update.message.reply_text(
            f"You haven't claimed anything for {w['label']} yet."
        )
        return
    lines = [f"<b>Your shifts — {w['label']}</b>"] + [
        f"{r['name']} {fmt_day(date.fromisoformat(r['the_date']))}: {r['label']}"
        for r in rows
    ]
    lines.append(f"\n{len(rows)} slot(s)")
    await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Use /schedule here, or message me directly.")
        return
    if not await gate(update):
        return
    w = latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return
    if BOARD_MODE == "single":
        text, _ = render_board(w["id"])
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)
        return
    lines = [render_header(w["id"])[0], ""]
    for d in q("SELECT * FROM days WHERE week_id=? ORDER BY idx", (w["id"],)):
        day_text, _ = render_day(d["id"])
        lines.append(day_text)
        lines.append("")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=constants.ParseMode.HTML
    )


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Repost the live board at the bottom of the group, still self-updating."""
    if not is_admin(update.effective_user.id):
        return
    w = open_week() or latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return
    if not GROUP_CHAT_ID:
        await update.message.reply_text("GROUP_CHAT_ID isn't set.")
        return

    bot = context.bot
    old_id = w["header_msg_id"]

    if BOARD_MODE == "single":
        text, kb = render_board(w["id"])
    else:
        text, kb = render_header(w["id"])
    msg = await send_group(
        bot, text, reply_markup=kb, parse_mode=constants.ParseMode.HTML
    )
    run("UPDATE weeks SET header_msg_id=? WHERE id=?", (msg.message_id, w["id"]))

    # Retire the old copy so there's only ever one live board.
    if old_id and old_id != msg.message_id:
        try:
            await bot.delete_message(GROUP_CHAT_ID, old_id)
        except Exception:
            try:
                await bot.edit_message_text(
                    chat_id=GROUP_CHAT_ID,
                    message_id=old_id,
                    text="📋 <i>The board has moved further down.</i>",
                    parse_mode=constants.ParseMode.HTML,
                )
            except Exception:
                pass

    try:
        await bot.pin_chat_message(GROUP_CHAT_ID, msg.message_id, disable_notification=True)
    except (BadRequest, Forbidden):
        pass

    if update.effective_chat.type == constants.ChatType.PRIVATE:
        await update.message.reply_text("Board reposted to the group ✅")


async def cmd_gaps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    w = latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return
    st = week_stats(w["id"])
    if not st["gaps"]:
        await update.message.reply_text("🎉 No gaps — every slot is covered.")
        return
    lines = [f"⚠️ <b>{len(st['gaps'])} unfilled slot(s) — {w['label']}</b>"] + [
        f"{g['name']} {fmt_day(date.fromisoformat(g['the_date']))}: {g['label']}"
        for g in st["gaps"]
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    w = open_week()
    if not w:
        await update.message.reply_text("No week is currently open.")
        return
    await nudge(context, w["id"], "Reminder")
    await update.message.reply_text("Nudge sent.")


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    w = open_week()
    if not w:
        await update.message.reply_text("No week is currently open.")
        return
    await close_week(context, w["id"])
    await update.message.reply_text("Week closed.")


async def cmd_roster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    rows = q("SELECT * FROM agents WHERE status='active' ORDER BY name")
    waiting = q1("SELECT COUNT(*) c FROM agents WHERE status='pending'")["c"]
    if not rows:
        await update.message.reply_text(
            "Roster is empty. Agents join it by tapping a slot, or by sending me /start."
        )
        return
    lines = [f"<b>Roster ({len(rows)})</b>"] + [
        f"{'🔔' if r['dm_ok'] else '🔕'} {esc(r['display_name'] or r['name'])}"
        + (f" @{r['username']}" if r["username"] else "")
        + f" · <code>{r['user_id']}</code>"
        for r in rows
    ]
    lines.append("\n🔕 = hasn't sent me /start, so can't get shift reminders.")
    lines.append("Remove someone with <code>/removeagent @handle</code> or their ID.")
    if waiting:
        lines.append(f"\n⏳ {waiting} waiting for approval — see /pending")
    await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)


async def _set_role(update, context, new_role: str) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Only an owner can change roles.")
        return
    if not context.args:
        verb = "makeadmin" if new_role == "admin" else "removeadmin"
        await update.message.reply_text(f"Usage: /{verb} @handle")
        return
    target = " ".join(context.args).strip()
    row = None
    if target.startswith("@"):
        row = q1("SELECT * FROM agents WHERE lower(username)=lower(?)", (target[1:],))
    elif target.isdigit():
        row = q1("SELECT * FROM agents WHERE user_id=?", (int(target),))
    if not row:
        row = q1("SELECT * FROM agents WHERE lower(name)=lower(?)", (target,))
    if not row:
        await update.message.reply_text(
            "No one on the roster matches that. Check /roster."
        )
        return
    if is_owner(row["user_id"]):
        await update.message.reply_text(
            "That person is an owner — set in config, so I can't change it here."
        )
        return

    run("UPDATE agents SET role=? WHERE user_id=?", (new_role, row["user_id"]))
    await refresh_menu_for(context.bot, row["user_id"])

    if new_role == "admin":
        await update.message.reply_text(f"{esc(row['name'])} is now an admin.")
        try:
            await context.bot.send_message(
                row["user_id"],
                "You've been given admin rights. Send /help to see what's new.",
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(f"{esc(row['name'])} is now a regular agent.")


async def cmd_makeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_role(update, context, "admin")


async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_role(update, context, "agent")


async def cmd_avails(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Take someone off the weekly avails roster without removing their access.

    Full-timers and anyone who doesn't pick slots shouldn't be tagged on the
    board or chased about confirming.
    """
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        on = q("SELECT * FROM agents WHERE status='active' AND on_avails=1 ORDER BY name")
        off = q("SELECT * FROM agents WHERE status='active' AND on_avails=0 ORDER BY name")
        lines = [f"<b>On the avails roster ({len(on)})</b>"]
        lines += [f"  {esc(a['display_name'] or a['name'])}" for a in on] or ["  nobody"]
        lines += ["", f"<b>Not tagged or chased ({len(off)})</b>"]
        lines += [f"  {esc(a['display_name'] or a['name'])}" for a in off] or ["  nobody"]
        lines += [
            "",
            "<code>/avails off @handle</code> — stop tagging them",
            "<code>/avails on @handle</code> — put them back",
        ]
        await update.message.reply_text(
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    mode = context.args[0].lower()
    if mode not in ("on", "off") or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/avails off @handle</code> or <code>/avails on @handle</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    row = find_agent(context.args[1])
    if not row:
        await update.message.reply_text("No one matches that. Check /roster.")
        return

    want = 1 if mode == "on" else 0
    async with write_lock:
        run("UPDATE agents SET on_avails=? WHERE user_id=?", (want, row["user_id"]))
    nm = row["display_name"] or row["name"]
    if want:
        msg = (f"<b>{esc(nm)}</b> is back on the avails roster — "
               "they'll be tagged and chased again.")
    else:
        msg = (f"<b>{esc(nm)}</b> is off the avails roster.\n"
               "They won't be tagged on the board or chased about confirming, "
               "but they keep full access and can still pick slots.")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

    w = open_week()
    if w:
        await refresh_group(context, w["id"])


async def cmd_removeagent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Take someone off the roster and free any slots they were holding."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/removeagent @handle</code>, or their numeric ID from /roster.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    target = " ".join(context.args).strip()
    row = None
    if target.startswith("@"):
        row = q1(
            "SELECT * FROM agents WHERE lower(username)=lower(?)", (target[1:],)
        )
    elif target.isdigit():
        row = q1("SELECT * FROM agents WHERE user_id=?", (int(target),))
    if not row:
        row = q1("SELECT * FROM agents WHERE lower(name)=lower(?)", (target,))
    if not row:
        await update.message.reply_text(
            f"No one on the roster matches {target!r}. Check /roster for exact handles and IDs."
        )
        return

    async with write_lock:
        run(
            "UPDATE agents SET active=0, status='declined' WHERE user_id=?",
            (row["user_id"],),
        )
        freed = 0
        w = open_week()
        if w:
            cur = run(
                """DELETE FROM signups WHERE user_id=? AND slot_id IN
                   (SELECT s.id FROM slots s JOIN days d ON d.id=s.day_id
                    WHERE d.week_id=?)""",
                (row["user_id"], w["id"]),
            )
            freed = cur.rowcount
            run(
                "DELETE FROM confirmations WHERE week_id=? AND user_id=?",
                (w["id"], row["user_id"]),
            )

    msg = f"Removed <b>{esc(row['name'])}</b> from the roster."
    if freed:
        msg += f"\nFreed {freed} slot(s) in the current week — they're open again."
    else:
        msg += "\nThey weren't holding any slots this week."
    msg += "\n\n<i>Past weeks keep their history.</i>"
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

    if w:
        await refresh_group(context, w["id"])


def parse_month(args) -> date:
    if args:
        try:
            return datetime.strptime(args[0], "%Y-%m").date()
        except ValueError:
            pass
    return now().date().replace(day=1)


async def cmd_setrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setrate 14.50  (team)  ·  /setrate 16 @handle  (one person)"""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        team = rate_for(-1, now().date())
        await update.message.reply_text(
            f"Current team rate: <b>{money(team)}</b>/hour\n\n"
            "Change it with <code>/setrate 14.50</code>\n"
            "One person: <code>/setrate 16 @handle</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    try:
        cents = round(float(context.args[0].lstrip("$")) * 100)
        if cents < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Give an amount like 14.50")
        return

    agent_id, who = None, "the whole team"
    if len(context.args) > 1:
        target = context.args[1]
        row = None
        if target.startswith("@"):
            row = q1("SELECT * FROM agents WHERE lower(username)=lower(?)", (target[1:],))
        elif target.isdigit():
            row = q1("SELECT * FROM agents WHERE user_id=?", (int(target),))
        if not row:
            await update.message.reply_text("No one matches that. Check /roster.")
            return
        agent_id = row["user_id"]
        who = row["display_name"] or row["name"]

    today = now().date()
    async with write_lock:
        run(
            "INSERT INTO pay_rates (agent_id, cents, effective_from, created_by, created_at) "
            "VALUES (?,?,?,?,?)",
            (agent_id, cents, today.isoformat(),
             update.effective_user.id, now().isoformat()),
        )
    await update.message.reply_text(
        f"Rate for <b>{esc(who)}</b> set to <b>{money(cents)}</b>/hour, "
        f"from {today.strftime('%-d %b %Y')}.\n\n"
        "<i>Earlier months keep the rate that applied then.</i>",
        parse_mode=constants.ParseMode.HTML,
    )


async def cmd_timesheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Everyone's hours for a month."""
    if not is_admin(update.effective_user.id):
        return
    first = parse_month(context.args)
    _, last = month_bounds(first)

    lines = [f"🕐 <b>Team hours — {first.strftime('%B %Y')}</b>", ""]
    total_min = total_cents = 0
    any_rows = False
    for a in q("SELECT * FROM agents WHERE status='active' ORDER BY name"):
        t = timesheet(a["user_id"], first, last)
        if not t["shifts"] and not t["open"]:
            continue
        any_rows = True
        nm = a["display_name"] or a["name"]
        row = f"{esc(nm)} — {t['days']}d · {hhmm(t['minutes'])}"
        if t["cents"]:
            row += f" · {money(t['cents'])}"
        if t["open"]:
            row += " ⏱"
        lines.append(row)
        total_min += t["minutes"]
        total_cents += t["cents"]
    if not any_rows:
        lines.append("Nothing logged this month.")
    else:
        lines += ["", f"<b>Total: {hhmm(total_min)}</b>"]
        if total_cents:
            lines.append(f"<b>Estimated cost: {money(total_cents)}</b>")
        lines.append("\n<i>Estimates only — not payroll figures.</i>")
    lines.append("\n/payroll for a CSV of every shift.")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=constants.ParseMode.HTML
    )


async def cmd_payroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """CSV: one row per shift — date, hours, rate, pay."""
    if not is_admin(update.effective_user.id):
        return
    first = parse_month(context.args)
    _, last = month_bounds(first)

    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow([
        "entry", "agent", "telegram_id", "date", "day", "slot",
        "clock_in", "clock_out", "actual_hours", "paid_hours",
        "rate", "pay", "status",
    ])
    rows_written = 0
    for a in q("SELECT * FROM agents ORDER BY name"):
        t = timesheet(a["user_id"], first, last)
        for sh in t["shifts"]:
            r = sh["row"]
            slot = q1(
                "SELECT s.label, d.name FROM slots s JOIN days d ON d.id=s.day_id "
                "WHERE s.id=?",
                (r["slot_id"],),
            ) if r["slot_id"] else None
            wr.writerow([
                r["id"],
                a["display_name"] or a["name"],
                a["user_id"],
                sh["date"].isoformat(),
                sh["date"].strftime("%a"),
                slot["label"] if slot else "unrostered",
                datetime.fromisoformat(r["clock_in"]).strftime("%H:%M"),
                datetime.fromisoformat(r["clock_out"]).strftime("%H:%M"),
                f"{sh['actual'] / 60:.2f}",
                f"{sh['minutes'] / 60:.2f}",
                f"{rate_for(a['user_id'], sh['date']) / 100:.2f}",
                f"{sh['cents'] / 100:.2f}",
                r["status"],
            ])
            rows_written += 1

    if not rows_written:
        await update.message.reply_text(
            f"No shifts logged in {first.strftime('%B %Y')}."
        )
        return
    data = io.BytesIO(buf.getvalue().encode())
    data.name = f"payroll_{first.strftime('%Y_%m')}.csv"
    await update.message.reply_document(
        data, filename=data.name,
        caption=f"{rows_written} shift(s) — {first.strftime('%B %Y')}. Estimates, not payroll.",
    )


def log_edit(entry_id: int, by: int, before: dict, after: dict, reason: str) -> None:
    run(
        "INSERT INTO time_edits (entry_id, changed_by, before, after, reason, changed_at) "
        "VALUES (?,?,?,?,?,?)",
        (entry_id, by, json.dumps(before), json.dumps(after), reason,
         now().isoformat()),
    )


def find_agent(target: str):
    if target.startswith("@"):
        return q1("SELECT * FROM agents WHERE lower(username)=lower(?)", (target[1:],))
    if target.isdigit():
        return q1("SELECT * FROM agents WHERE user_id=?", (int(target),))
    return q1("SELECT * FROM agents WHERE lower(display_name)=lower(?) "
              "OR lower(name)=lower(?)", (target, target))


def at_time_on(the_date: str, hhmm_text: str) -> datetime | None:
    try:
        mins = parse_time_token(hhmm_text)
    except ValueError:
        return None
    d = date.fromisoformat(the_date)
    return datetime.combine(d, time(mins // 60, mins % 60), TZ)


async def cmd_openshifts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Who is clocked in right now."""
    if not is_admin(update.effective_user.id):
        return
    rows = q(
        "SELECT * FROM time_entries WHERE clock_out IS NULL ORDER BY clock_in"
    )
    if not rows:
        await update.message.reply_text("Nobody is clocked in right now.")
        return
    lines = [f"⏱ <b>{len(rows)} shift(s) still open</b>", ""]
    for r in rows:
        nm = display_name_of(r["agent_id"], str(r["agent_id"]))
        started = datetime.fromisoformat(r["clock_in"])
        mins = int((now() - started).total_seconds() // 60)
        lines.append(
            f"<b>{esc(nm)}</b> — since {started.strftime('%-d %b %H:%M')} "
            f"({hhmm(mins)} ago)\n"
            f"   <code>/clockoutfor {r['agent_id']} 18:00</code>"
        )
    await update.message.reply_text(
        "\n".join(lines), parse_mode=constants.ParseMode.HTML
    )


async def cmd_clockoutfor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clockoutfor @handle 18:00 — close someone's forgotten shift."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/clockoutfor @handle 18:00</code>\n"
            "Leave the time out to use now. See /openshifts.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    row = find_agent(context.args[0])
    if not row:
        await update.message.reply_text("No one matches that. Check /roster.")
        return

    entry = q1(
        "SELECT * FROM time_entries WHERE agent_id=? AND clock_out IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (row["user_id"],),
    )
    if not entry:
        await update.message.reply_text(
            f"{esc(row['display_name'] or row['name'])} isn't clocked in.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    if len(context.args) > 1:
        out = at_time_on(entry["the_date"], context.args[1])
        if not out:
            await update.message.reply_text("Give a time like 18:00 or 6pm.")
            return
    else:
        out = now()

    started = datetime.fromisoformat(entry["clock_in"])
    if out <= started:
        out += timedelta(days=1)
    if out <= started:
        await update.message.reply_text("That's before they clocked in.")
        return

    async with write_lock:
        log_edit(
            entry["id"], update.effective_user.id,
            {"clock_out": None, "status": entry["status"]},
            {"clock_out": out.isoformat(), "status": "edited"},
            "admin clocked out on their behalf",
        )
        run(
            "UPDATE time_entries SET clock_out=?, status='edited', source='admin' "
            "WHERE id=?",
            (out.isoformat(), entry["id"]),
        )

    fresh = q1("SELECT * FROM time_entries WHERE id=?", (entry["id"],))
    mins = entry_minutes(fresh)
    nm = row["display_name"] or row["name"]
    await update.message.reply_text(
        f"✅ Clocked out <b>{esc(nm)}</b> at {out.strftime('%-d %b %H:%M')}\n"
        f"Shift logged: <b>{hhmm(mins)}</b>\n\n"
        f"<i>Entry #{entry['id']} · change recorded</i>",
        parse_mode=constants.ParseMode.HTML,
    )
    try:
        await context.bot.send_message(
            row["user_id"],
            f"Your manager clocked you out at {out.strftime('%H:%M')} "
            f"({hhmm(mins)} logged).\n\nCheck /mytime — tell them if it's wrong.",
        )
    except Exception:
        pass


async def cmd_fixtime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fixtime 42 10:00 18:00 — correct an entry. /fixtime 42 delete removes it."""
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n"
            "<code>/fixtime 42 10:00 18:00</code> — set both times\n"
            "<code>/fixtime 42 delete</code> — remove the entry\n\n"
            "Entry numbers show in /mytime and /payroll.",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    if not context.args[0].isdigit():
        await update.message.reply_text("First give the entry number, e.g. /fixtime 42 ...")
        return

    entry = q1("SELECT * FROM time_entries WHERE id=?", (int(context.args[0]),))
    if not entry:
        await update.message.reply_text("No entry with that number.")
        return
    nm = display_name_of(entry["agent_id"], str(entry["agent_id"]))

    if context.args[1].lower() == "delete":
        async with write_lock:
            log_edit(
                entry["id"], update.effective_user.id,
                {"clock_in": entry["clock_in"], "clock_out": entry["clock_out"]},
                {"deleted": True}, "admin deleted the entry",
            )
            run("DELETE FROM time_entries WHERE id=?", (entry["id"],))
        await update.message.reply_text(
            f"Deleted entry #{entry['id']} for <b>{esc(nm)}</b>.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    if len(context.args) < 3:
        await update.message.reply_text("Give both times: /fixtime 42 10:00 18:00")
        return
    a = at_time_on(entry["the_date"], context.args[1])
    b = at_time_on(entry["the_date"], context.args[2])
    if not a or not b:
        await update.message.reply_text("Give times like 10:00 and 18:00.")
        return
    if b <= a:
        b += timedelta(days=1)

    async with write_lock:
        log_edit(
            entry["id"], update.effective_user.id,
            {"clock_in": entry["clock_in"], "clock_out": entry["clock_out"]},
            {"clock_in": a.isoformat(), "clock_out": b.isoformat()},
            "admin corrected the times",
        )
        run(
            "UPDATE time_entries SET clock_in=?, clock_out=?, status='edited', "
            "source='admin' WHERE id=?",
            (a.isoformat(), b.isoformat(), entry["id"]),
        )

    fresh = q1("SELECT * FROM time_entries WHERE id=?", (entry["id"],))
    mins = entry_minutes(fresh)
    await update.message.reply_text(
        f"✅ Entry #{entry['id']} for <b>{esc(nm)}</b> is now "
        f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')} (<b>{hhmm(mins)}</b>).\n\n"
        "<i>Change recorded.</i>",
        parse_mode=constants.ParseMode.HTML,
    )
    try:
        await context.bot.send_message(
            entry["agent_id"],
            f"Your manager corrected your shift on "
            f"{date.fromisoformat(entry['the_date']).strftime('%-d %b')} to "
            f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')} ({hhmm(mins)}).\n\n"
            "Check /mytime — tell them if it's wrong.",
        )
    except Exception:
        pass


def snapshot_db() -> tuple[io.BytesIO, dict]:
    """A consistent copy of the database, safe to take while the bot is running."""
    import tempfile

    counts = {}
    for t in ("agents", "weeks", "signups", "time_entries", "pay_rates"):
        try:
            counts[t] = q1(f"SELECT COUNT(*) c FROM {t}")["c"]
        except Exception:
            counts[t] = 0

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    dest = sqlite3.connect(tmp.name)
    with dest:
        db.backup(dest)
    dest.close()

    with open(tmp.name, "rb") as fh:
        data = io.BytesIO(fh.read())
    os.unlink(tmp.name)
    data.name = f"avails_{now().strftime('%Y-%m-%d_%H%M')}.db"
    return data, counts


def backup_caption(counts: dict) -> str:
    return (
        f"🗄 Backup — {now().strftime('%-d %b %Y, %H:%M')}\n"
        f"{counts['agents']} agents · {counts['weeks']} weeks · "
        f"{counts['signups']} signups · {counts['time_entries']} shifts\n\n"
        "Keep this somewhere safe. It's the only copy outside the server."
    )


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    try:
        data, counts = snapshot_db()
    except Exception as e:
        await update.message.reply_text(f"Backup failed: {e}")
        log.warning("Manual backup failed: %s", e)
        return
    await update.message.reply_document(
        data, filename=data.name, caption=backup_caption(counts)
    )


async def job_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Weekly database copy, DM'd to owners."""
    if now().weekday() != BACKUP_DAY:
        return
    targets = list(ADMIN_IDS) or admin_ids()
    if not targets:
        log.warning("Backup due but no owner to send it to.")
        return
    try:
        data, counts = snapshot_db()
    except Exception as e:
        log.warning("Scheduled backup failed: %s", e)
        return
    body = data.getvalue()
    for uid in targets:
        try:
            await context.bot.send_document(
                uid, io.BytesIO(body), filename=data.name,
                caption=backup_caption(counts),
            )
        except Exception as e:
            log.info("Couldn't send backup to %s: %s", uid, e)
    log.info("Backup sent to %d owner(s).", len(targets))


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear test schedules and time entries. Keeps people and rates."""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Only an owner can reset the data.")
        return

    counts = {
        "weeks": q1("SELECT COUNT(*) c FROM weeks")["c"],
        "signups": q1("SELECT COUNT(*) c FROM signups")["c"],
        "shifts": q1("SELECT COUNT(*) c FROM time_entries")["c"],
        "agents": q1("SELECT COUNT(*) c FROM agents WHERE status='active'")["c"],
    }

    if not context.args or context.args[0] != "CONFIRM":
        await update.message.reply_text(
            "⚠️ <b>This will permanently delete:</b>\n"
            f"  · {counts['weeks']} week(s) and every slot in them\n"
            f"  · {counts['signups']} slot claim(s)\n"
            f"  · {counts['shifts']} clock-in record(s)\n\n"
            "<b>It will keep:</b>\n"
            f"  · your {counts['agents']} approved agent(s)\n"
            "  · pay rates and timing presets\n\n"
            "I'll send you a backup first.\n\n"
            "To go ahead, send <code>/reset CONFIRM</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    try:
        data, c = snapshot_db()
        await update.message.reply_document(
            data, filename=data.name,
            caption="🗄 Backup taken before reset. Keep this.",
        )
    except Exception as e:
        await update.message.reply_text(
            f"Couldn't take a backup ({e}) — stopping rather than deleting blind."
        )
        return

    async with write_lock:
        for t in ("time_edits", "time_entries", "confirmations",
                  "signups", "slots", "days", "weeks"):
            run(f"DELETE FROM {t}")
        db.execute("DELETE FROM sqlite_sequence WHERE name IN "
                   "('weeks','days','slots','time_entries','time_edits')")
        db.commit()

    await update.message.reply_text(
        "✅ <b>Cleared.</b>\n\n"
        f"{counts['agents']} agent(s) kept — they don't need to re-register.\n"
        "Pay rates kept.\n\n"
        "Post your first real week with /newweek.",
        parse_mode=constants.ParseMode.HTML,
    )
    log.info("Data reset by owner %s", update.effective_user.id)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    w = latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return
    rows = q(
        """SELECT d.name AS day, d.the_date, s.label, su.name AS agent
           FROM slots s JOIN days d ON d.id = s.day_id
           LEFT JOIN signups su ON su.slot_id = s.id
           WHERE d.week_id=? ORDER BY d.idx, s.idx, su.ts""",
        (w["id"],),
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["day", "date", "slot", "agent"])
    for r in rows:
        writer.writerow([r["day"], r["the_date"], r["label"], r["agent"] or ""])
    data = io.BytesIO(buf.getvalue().encode())
    data.name = f"{w['label'].replace(' ', '_')}_avails.csv"
    await update.message.reply_document(data, filename=data.name)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Use /schedule here, or message me directly.")
        return

    role = role_of(update.effective_user.id)
    lines = [
        "<b>What I can do</b>",
        "",
        "/plan — fill in your week, day by day",
        "/myshifts — what you're signed up for",
        "/summary — the current board",
    ]
    if role in ("admin", "owner"):
        lines += [
            "",
            "<b>Admin</b>",
            "/newweek — set up and post a week",
            "/schedule — post the board to the group",
            "/gaps — unfilled slots",
            "/remind — nudge whoever hasn't confirmed",
            "/shiftcall — post tomorrow's on-duty tags now",
            "/roster — who's on the list",
            "/removeagent — remove someone, freeing their slots",
            "/export — download this week as CSV",
            "/presets — saved timing patterns",
            "/closeweek — close submissions early",
        ]
    if role == "owner":
        lines += [
            "",
            "<b>Owner</b>",
            "/makeadmin — give someone admin rights",
            "/removeadmin — take them away",
            "/chatid — show this chat's ID",
        ]
    await update.message.reply_text(
        "\n".join(lines), parse_mode=constants.ParseMode.HTML
    )



# --------------------------------------------------------------------------
# Mini App
# --------------------------------------------------------------------------

MINIAPP_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>My hours</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px 16px 40px;
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--tg-theme-bg-color, #fff);
    color: var(--tg-theme-text-color, #111);
  }
  h1 { font-size: 20px; margin: 0 0 2px; }
  .sub { color: var(--tg-theme-hint-color, #777); font-size: 13px; margin-bottom: 18px; }
  .cards { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 22px; }
  .card {
    background: var(--tg-theme-secondary-bg-color, #f4f4f5);
    border-radius: 14px; padding: 14px;
  }
  .card .n { font-size: 22px; font-weight: 650; letter-spacing: -0.02em; }
  .card .l { font-size: 12px; color: var(--tg-theme-hint-color, #777); margin-top: 2px; }
  .card.wide { grid-column: 1 / -1; }
  .card.pay .n { color: var(--tg-theme-link-color, #2a7); }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em;
       color: var(--tg-theme-hint-color, #777); margin: 0 0 8px; font-weight: 600; }
  .row { display: flex; justify-content: space-between; align-items: baseline;
         padding: 11px 0; border-bottom: 1px solid var(--tg-theme-secondary-bg-color, #eee); }
  .row:last-child { border-bottom: 0; }
  .d { font-weight: 550; }
  .t { font-size: 12px; color: var(--tg-theme-hint-color, #777); margin-top: 1px; }
  .h { font-variant-numeric: tabular-nums; }
  .flag { font-size: 11px; color: #c60; }
  .empty { text-align: center; padding: 40px 20px; color: var(--tg-theme-hint-color, #777); }
  .note { margin-top: 22px; font-size: 12px; color: var(--tg-theme-hint-color, #777);
          line-height: 1.5; }
  .err { background: #fee; color: #900; padding: 14px; border-radius: 12px; }
</style>
</head><body>
<div id="app"><div class="empty">Loading…</div></div>
<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

async function load() {
  const app = document.getElementById('app');
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 15000);
    const r = await fetch('/api/me', {
      headers: { 'X-Init-Data': tg?.initData || '' },
      signal: ctrl.signal
    });
    clearTimeout(timer);
    if (r.status === 401) {
      app.innerHTML = '<div class="err">Could not verify who you are. Open this from the bot rather than a browser.</div>';
      return;
    }
    if (r.status === 403) {
      app.innerHTML = '<div class="err">You do not have access yet. Send /start to the bot.</div>';
      return;
    }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();

    let h = `<h1>${esc(d.name)}</h1><div class="sub">${esc(d.month)}</div>`;
    h += '<div class="cards">';
    h += `<div class="card"><div class="n">${esc(d.hours)}</div><div class="l">Hours worked</div></div>`;
    h += `<div class="card"><div class="n">${d.days}</div><div class="l">Days worked</div></div>`;
    if (d.showPay) {
      h += `<div class="card pay wide"><div class="n">${esc(d.estimate)}</div>`;
      h += `<div class="l">Estimated · ${esc(d.rate)}/hour</div></div>`;
    }
    h += '</div>';

    if (d.shifts.length) {
      h += '<h2>Shifts</h2>';
      for (const s of d.shifts) {
        h += '<div class="row"><div>';
        h += `<div class="d">${esc(s.date)}</div>`;
        h += `<div class="t">${esc(s.times)}${s.flagged ? ' <span class="flag">· auto-closed</span>' : ''}</div>`;
        h += `</div><div class="h">${esc(s.hours)}</div></div>`;
      }
    } else {
      h += '<div class="empty">No shifts logged this month yet.<br>Use /clockin when you start.</div>';
    }
    if (d.openShift) {
      h += '<div class="note">⏱ You are clocked in right now — this shift is not counted yet.</div>';
    }
    if (d.showPay) {
      h += '<div class="note">This is an estimate to help you plan. Your actual pay comes from HR and may differ.</div>';
    }
    app.innerHTML = h;
  } catch (e) {
    const why = e.name === 'AbortError' ? 'The server did not answer in time.' : esc(e.message || e);
    app.innerHTML = '<div class="err">Could not load your hours.<br><br>' + why + '</div>';
  }
}
load();
</script>
</body></html>"""


def miniapp_payload(user_id: int) -> dict:
    """Built on a connection of its own — this runs on the web thread, not the
    bot's, and sharing one SQLite connection across threads is unsafe."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        agent = conn.execute(
            "SELECT display_name, name FROM agents WHERE user_id=?", (user_id,)
        ).fetchone()
        name = (agent["display_name"] or agent["name"] or "You") if agent else "You"

        today = now().date()
        first = today.replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)

        def rate_on(d: date) -> int:
            row = conn.execute(
                "SELECT cents FROM pay_rates WHERE agent_id=? AND effective_from<=? "
                "ORDER BY effective_from DESC, id DESC LIMIT 1",
                (user_id, d.isoformat()),
            ).fetchone()
            if row:
                return row["cents"]
            row = conn.execute(
                "SELECT cents FROM pay_rates WHERE agent_id IS NULL AND effective_from<=? "
                "ORDER BY effective_from DESC, id DESC LIMIT 1",
                (d.isoformat(),),
            ).fetchone()
            return row["cents"] if row else DEFAULT_RATE_CENTS

        rows = conn.execute(
            "SELECT * FROM time_entries WHERE agent_id=? AND the_date BETWEEN ? AND ? "
            "ORDER BY the_date, clock_in",
            (user_id, first.isoformat(), last.isoformat()),
        ).fetchall()

        shifts, total_min, total_cents, open_count = [], 0, 0, 0
        seen_days = set()
        for r in rows:
            if not r["clock_out"]:
                open_count += 1
                continue
            a = datetime.fromisoformat(r["clock_in"])
            b = datetime.fromisoformat(r["clock_out"])
            actual = max(0, int((b - a).total_seconds() // 60))
            mins = entry_paid_minutes(r)
            day = date.fromisoformat(r["the_date"])
            total_min += mins
            total_cents += round(mins / 60 * rate_on(day)) if mins else 0
            seen_days.add(day)
            shifts.append({
                "date": day.strftime("%a %-d %b"),
                "times": f"{a.strftime('%H:%M')}\u2013{b.strftime('%H:%M')}",
                "hours": hhmm(mins),
                "flagged": r["status"] == "auto",
            })
        rate = rate_on(today)
    finally:
        conn.close()

    shifts.reverse()
    return {
        "name": name,
        "month": first.strftime("%B %Y"),
        "hours": hhmm(total_min),
        "days": len(seen_days),
        "showPay": bool(rate),
        "rate": money(rate),
        "estimate": money(total_cents),
        "openShift": bool(open_count),
        "shifts": shifts,
    }


def web_has_access(user_id: int) -> bool:
    """Access check on the web thread's own connection."""
    if user_id in ADMIN_IDS:
        return True
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        row = conn.execute(
            "SELECT status FROM agents WHERE user_id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if row:
        return row[0] == "active"
    return not ADMIN_IDS


class MiniAppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            self._route()
        except Exception as e:
            log.warning("Mini App handler error: %s", e, exc_info=True)
            try:
                self._send(500, b'{"error":"server"}')
            except Exception:
                pass

    def _route(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, MINIAPP_HTML.encode(), "text/html; charset=utf-8")
            return
        if path == "/health":
            self._send(200, b'{"ok":true}')
            return
        if path == "/api/me":
            try:
                raw = self.headers.get("X-Init-Data", "")
                user = verify_init_data(raw)
                if not user or "id" not in user:
                    self._send(401, b'{"error":"unverified"}')
                    return
                uid = int(user["id"])
                if not web_has_access(uid):
                    self._send(403, b'{"error":"no access"}')
                    return
                body = json.dumps(miniapp_payload(uid)).encode()
                self._send(200, body)
            except Exception as e:
                log.warning("Mini App request failed: %s", e, exc_info=True)
                try:
                    self._send(500, b'{"error":"server"}')
                except Exception:
                    pass
            return
        self._send(404, b'{"error":"not found"}')

    def log_message(self, *a):
        pass


def start_web_server() -> None:
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), MiniAppHandler)
    except Exception as e:
        log.warning("Mini App server could not start: %s", e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Mini App server listening on port %s", WEB_PORT)


async def cmd_payslip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the hours summary."""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    if not await gate(update):
        return
    if not PUBLIC_URL:
        await update.message.reply_text(
            "The app isn't set up yet. Use /mytime for now."
        )
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "📊 Open my hours", web_app=WebAppInfo(url=PUBLIC_URL)
        )]]
    )
    await update.message.reply_text(
        "Your hours and estimated pay for this month:", reply_markup=kb
    )


# --------------------------------------------------------------------------
# Scheduled jobs
# --------------------------------------------------------------------------


async def nudge(context: ContextTypes.DEFAULT_TYPE, week_id: int, prefix: str) -> None:
    w = q1("SELECT * FROM weeks WHERE id=?", (week_id,))
    if not w or w["status"] != "open":
        return
    st = week_stats(week_id)
    dl = datetime.fromisoformat(w["deadline"])
    parts = [f"⏰ <b>{prefix} — {w['label']} closes {dl.strftime('%a %H:%M')}</b>"]

    if st["missing"]:
        names = " ".join(
            f"@{a['username']}" if a["username"] else a["name"] for a in st["missing"]
        )
        parts.append(
            f"\nNot yet confirmed: {names}"
            "\n<i>Check the pinned board and hit ✅ Slots.</i>"
        )
    if st["gaps"]:
        preview = ", ".join(
            f"{g['name']} {short_label(g['label'])}" for g in st["gaps"][:8]
        )
        more = f" +{len(st['gaps']) - 8} more" if len(st["gaps"]) > 8 else ""
        parts.append(f"\n⚠️ Still uncovered: {preview}{more}")
    if not st["missing"] and not st["gaps"]:
        return

    await send_group(
        context.bot, "\n".join(parts), parse_mode=constants.ParseMode.HTML
    )


async def job_nudge(context: ContextTypes.DEFAULT_TYPE) -> None:
    await nudge(context, context.job.data["week_id"], context.job.data["prefix"])


async def close_week(context: ContextTypes.DEFAULT_TYPE, week_id: int) -> None:
    w = q1("SELECT * FROM weeks WHERE id=?", (week_id,))
    if not w or w["status"] != "open":
        return
    run("UPDATE weeks SET status='closed' WHERE id=?", (week_id,))
    if BOARD_MODE == "single":
        await refresh_board(context, week_id)
    else:
        for d in q("SELECT id FROM days WHERE week_id=?", (week_id,)):
            await refresh_day(context, d["id"])
        await refresh_header(context, week_id)

    st = week_stats(week_id)
    msg = [f"🔒 <b>{w['label']} submissions are closed.</b>"]
    if st["gaps"]:
        msg.append(f"\n⚠️ {len(st['gaps'])} slot(s) still need cover:")
        msg += [
            f"  {g['name']} {fmt_day(date.fromisoformat(g['the_date']))}: {g['label']}"
            for g in st["gaps"]
        ]
    else:
        msg.append("\n🎉 Fully covered. Nice work.")
    await send_group(
        context.bot, "\n".join(msg), parse_mode=constants.ParseMode.HTML
    )


async def job_close(context: ContextTypes.DEFAULT_TYPE) -> None:
    await close_week(context, context.job.data["week_id"])


def mention(user_id: int, name: str) -> str:
    """@handle if they have one, otherwise a clickable name that still pings."""
    a = q1("SELECT username FROM agents WHERE user_id=?", (user_id,))
    if a and a["username"]:
        return f"@{a['username']}"
    return f'<a href="tg://user?id={user_id}">{esc(name)}</a>'


async def job_auto_close(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Close forgotten clock-ins, but only once the agent's whole run of
    back-to-back shifts has finished."""
    cutoff = now() - timedelta(minutes=AUTO_CLOSE_GRACE_MIN)
    for r in q("SELECT * FROM time_entries WHERE clock_out IS NULL"):
        started = datetime.fromisoformat(r["clock_in"])
        if started > cutoff:
            continue

        end = None
        if r["slot_id"]:
            run_ = slot_run_from(r["slot_id"])
            if run_:
                last = run_[-1]
                finish_min = last["start_min"] + slot_minutes(last["label"])
                day = date.fromisoformat(r["the_date"])
                end = datetime.combine(day, time(0, 0), TZ) + timedelta(minutes=finish_min)

        # Still mid-run — leave them clocked in.
        if end and end > now() - timedelta(minutes=AUTO_CLOSE_GRACE_MIN):
            continue
        if end is None or end <= started:
            end = started + timedelta(hours=2)

        async with write_lock:
            run(
                "UPDATE time_entries SET clock_out=?, status='auto', "
                "note='auto-closed, agent did not clock out' WHERE id=?",
                (end.isoformat(), r["id"]),
            )
        fresh = q1("SELECT * FROM time_entries WHERE id=?", (r["id"],))
        paid = entry_paid_minutes(fresh)
        try:
            await context.bot.send_message(
                r["agent_id"],
                f"⚠️ You didn't clock out, so I've closed your shift at "
                f"{end.strftime('%H:%M')} based on your roster "
                f"({hhmm(paid)} credited).\n\n"
                "If that's wrong, tell your manager and they can correct it.",
            )
        except Exception:
            pass
        log.info("Auto-closed entry %s for %s", r["id"], r["agent_id"])


async def job_shift_call(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Evening-before group post tagging tomorrow's agents."""
    text, reason = build_shift_call()
    if not text:
        log.info("Shift call skipped: %s", reason)
        return
    try:
        await send_group(
            context.bot, text, thread=SHIFTCALL_THREAD_ID,
            parse_mode=constants.ParseMode.HTML,
        )
    except Exception as e:
        log.warning("Shift call failed: %s", e)


def build_shift_call(for_date: date | None = None) -> tuple[str | None, str]:
    """Returns (message, reason). Message is None when there's nothing to send."""
    target = for_date or (now() + timedelta(days=1)).date()
    if not GROUP_CHAT_ID:
        return None, "GROUP_CHAT_ID isn't set."
    day = q1("SELECT * FROM days WHERE the_date=?", (target.isoformat(),))
    if not day:
        return None, f"No posted week covers {target.isoformat()}."

    lines = [f"\U0001F4E2 <b>On duty tomorrow \u2014 {day['name']} {fmt_day(target)}</b>", ""]
    working: dict[int, str] = {}
    gaps = 0
    for s_ in q("SELECT * FROM slots WHERE day_id=? ORDER BY idx", (day["id"],)):
        holders = slot_holders(s_["id"])
        if holders:
            lines.append(f"{s_['label']}: " + esc(", ".join(h["name"] for h in holders)))
            for h in holders:
                working[h["user_id"]] = h["name"]
        else:
            lines.append(f"{s_['label']}: \u2014")
            gaps += 1

    if not working:
        return None, f"Nobody has claimed any slot on {day['name']} {fmt_day(target)}."
    if gaps:
        lines.append(f"\n\u26A0\uFE0F {gaps} slot(s) with nobody on")
    lines.append("\n" + " ".join(mention(u, n) for u, n in working.items()))
    return "\n".join(lines), "ok"


async def job_shift_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every evening, DM everyone working tomorrow."""
    tomorrow = (now() + timedelta(days=1)).date()
    rows = q(
        """SELECT su.user_id, su.name, s.label, s.start_min, d.name AS day_name
           FROM signups su JOIN slots s ON s.id = su.slot_id
           JOIN days d ON d.id = s.day_id
           WHERE d.the_date = ? ORDER BY s.start_min""",
        (tomorrow.isoformat(),),
    )
    if not rows:
        return
    by_user: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append(r)

    for user_id, shifts in by_user.items():
        agent = q1("SELECT * FROM agents WHERE user_id=?", (user_id,))
        if agent and not agent["dm_ok"]:
            continue
        slots = "\n".join(f"  • {s['label']}" for s in shifts)
        text = (
            f"👋 Reminder — you're on livechat tomorrow, "
            f"{shifts[0]['day_name']} {fmt_day(tomorrow)}:\n{slots}\n\n"
            "Can't make it? Let the group know as early as you can."
        )
        try:
            await context.bot.send_message(user_id, text)
        except Forbidden:
            run("UPDATE agents SET dm_ok=0 WHERE user_id=?", (user_id,))
        except Exception as e:
            log.warning("Reminder to %s failed: %s", user_id, e)
        await asyncio.sleep(0.05)


def schedule_week_jobs(app: Application, week_id: int) -> None:
    w = q1("SELECT * FROM weeks WHERE id=?", (week_id,))
    if not w or w["status"] != "open":
        return
    jq = app.job_queue
    dl = datetime.fromisoformat(w["deadline"])

    for name in (f"nudge1-{week_id}", f"nudge2-{week_id}", f"close-{week_id}"):
        for job in jq.get_jobs_by_name(name):
            job.schedule_removal()

    for offset, prefix, name in [
        (timedelta(days=-2), "Reminder", f"nudge1-{week_id}"),
        (timedelta(hours=-12), "Last call", f"nudge2-{week_id}"),
    ]:
        when = dl + offset
        if when > now():
            jq.run_once(
                job_nudge, when, name=name, data={"week_id": week_id, "prefix": prefix}
            )
    if dl > now():
        jq.run_once(job_close, dl, name=f"close-{week_id}", data={"week_id": week_id})


AGENT_COMMANDS = [
    ("plan", "Fill in my week"),
    ("clockin", "Start my shift"),
    ("clockout", "End my shift"),
    ("mytime", "My hours this month"),
    ("support", "Who I list as support"),
    ("payslip", "My hours and estimated pay"),
    ("myshifts", "What I'm signed up for"),
    ("summary", "Show the current board"),
    ("help", "List commands"),
]
GROUP_COMMANDS = [
    ("schedule", "Show this week's schedule"),
]
OWNER_EXTRA = [
    ("makeadmin", "Give someone admin rights"),
    ("removeadmin", "Take admin rights away"),
    ("chatid", "Show this chat's ID"),
]
ADMIN_COMMANDS = AGENT_COMMANDS + [
    ("newweek", "Set up and post a week"),
    ("gaps", "Unfilled slots"),
    ("remind", "Nudge the unconfirmed"),
    ("shiftcall", "Post tomorrow's on-duty tags"),
    ("roster", "Who's on the list"),
    ("removeagent", "Remove someone"),
    ("avails", "Who gets tagged for avails"),
    ("export", "Download this week as CSV"),
    ("presets", "Saved timing patterns"),
    ("closeweek", "Close submissions early"),
    ("timesheet", "Team hours this month"),
    ("payroll", "Export shifts as CSV"),
    ("setrate", "Set the hourly rate"),
    ("openshifts", "Who's clocked in now"),
    ("clockoutfor", "Clock someone out"),
    ("fixtime", "Correct a time entry"),
    ("backup", "Download a copy of the data"),
    ("reset", "Clear test data before launch"),
]


async def refresh_menu_for(bot, user_id: int) -> None:
    """Re-publish one person's menu after their role changes."""
    role = role_of(user_id)
    pairs = ADMIN_COMMANDS if role in ("admin", "owner") else AGENT_COMMANDS
    if role == "owner":
        pairs = pairs + OWNER_EXTRA
    try:
        await bot.set_my_commands(
            [BotCommand(c, d) for c, d in pairs],
            scope=BotCommandScopeChat(user_id),
        )
    except Exception as e:
        log.info("Couldn't refresh menu for %s: %s", user_id, e)


async def publish_command_menus(app: Application) -> None:
    """Different menus in groups, DMs, and admin DMs."""
    bot = app.bot
    def cmds(pairs):
        return [BotCommand(c, d) for c, d in pairs]
    try:
        await bot.set_my_commands(cmds(GROUP_COMMANDS), scope=BotCommandScopeDefault())
        await bot.set_my_commands(
            cmds(GROUP_COMMANDS), scope=BotCommandScopeAllGroupChats()
        )
        if ADMIN_IDS:
            await bot.set_my_commands(
                cmds(AGENT_COMMANDS), scope=BotCommandScopeAllPrivateChats()
            )
            elevated = set(ADMIN_IDS) | {
                r["user_id"]
                for r in q("SELECT user_id FROM agents WHERE role='admin' AND active=1")
            }
            for uid in elevated:
                await refresh_menu_for(bot, uid)
            log.info("Elevated menus published for %d user(s).", len(elevated))
        else:
            # No admins configured, so everyone sees everything.
            await bot.set_my_commands(
                cmds(ADMIN_COMMANDS), scope=BotCommandScopeAllPrivateChats()
            )
        log.info("Command menus published.")
    except Exception as e:
        log.warning("Could not publish command menus: %s", e)


async def post_init(app: Application) -> None:
    """Re-arm jobs after a restart."""
    start_web_server()
    if PUBLIC_URL:
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="My hours", web_app=WebAppInfo(url=PUBLIC_URL)
                )
            )
            log.info("Mini App button set to %s", PUBLIC_URL)
        except Exception as e:
            log.info("Couldn't set the menu button: %s", e)
    await publish_command_menus(app)
    w = open_week()
    if w:
        schedule_week_jobs(app, w["id"])
        log.info("Re-armed jobs for %s", w["label"])
    mins = parse_time_token(SHIFT_CALL_TIME)
    app.job_queue.run_daily(
        job_shift_call,
        time(mins // 60, mins % 60, tzinfo=TZ),
        name="shift-call",
    )
    log.info("Group shift call scheduled for %s daily", SHIFT_CALL_TIME)
    app.job_queue.run_repeating(
        job_auto_close, interval=timedelta(minutes=30),
        first=timedelta(minutes=5), name="auto-close",
    )
    bmins = parse_time_token(BACKUP_TIME)
    app.job_queue.run_daily(
        job_backup, time(bmins // 60, bmins % 60, tzinfo=TZ), name="backup",
    )
    log.info(
        "Weekly backup scheduled for %s at %s",
        ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][BACKUP_DAY % 7],
        BACKUP_TIME,
    )
    if DM_REMINDERS:
        app.job_queue.run_daily(
            job_shift_reminders,
            time(SHIFT_REMINDER_HOUR, 0, tzinfo=TZ),
            name="shift-reminders",
        )


# --------------------------------------------------------------------------


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("newweek", newweek)],
            states={
                ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_date)],
                ASK_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_label)],
                ASK_EVENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_events)],
                ASK_SLOTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_slots)],
                ASK_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_deadline)],
                CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", cmd_start)],
            states={
                ASK_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)
                ]
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CommandHandler("shiftcall", cmd_shiftcall))
    app.add_handler(CommandHandler("myshifts", cmd_myshifts))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("gaps", cmd_gaps))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("closeweek", cmd_close))
    app.add_handler(CommandHandler("roster", cmd_roster))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("clockin", cmd_clockin))
    app.add_handler(CommandHandler("clockout", cmd_clockout))
    app.add_handler(CommandHandler("mytime", cmd_mytime))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("payslip", cmd_payslip))
    app.add_handler(CommandHandler("setrate", cmd_setrate))
    app.add_handler(CommandHandler("timesheet", cmd_timesheet))
    app.add_handler(CommandHandler("payroll", cmd_payroll))
    app.add_handler(CommandHandler("openshifts", cmd_openshifts))
    app.add_handler(CommandHandler("clockoutfor", cmd_clockoutfor))
    app.add_handler(CommandHandler("fixtime", cmd_fixtime))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("removeagent", cmd_removeagent))
    app.add_handler(CommandHandler("avails", cmd_avails))
    app.add_handler(CommandHandler("makeadmin", cmd_makeadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("presets", cmd_presets))
    app.add_handler(CommandHandler("savepreset", cmd_savepreset))
    app.add_handler(CallbackQueryHandler(on_toggle, pattern=r"^t:\d+$"))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^[cu]:\d+$"))
    app.add_handler(CallbackQueryHandler(on_plan_toggle, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(on_plan_nav, pattern=r"^pd:"))
    app.add_handler(CallbackQueryHandler(on_access_decision, pattern=r"^(ap|dn):\d+$"))
    app.add_handler(CallbackQueryHandler(on_plan_quick, pattern=r"^pq:"))

    log.info("Avails bot running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
