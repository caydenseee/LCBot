#!/usr/bin/env python3
"""
Avails Bot — weekly availability collection for the SH livechat team.

One message per day is posted to the group. Each has tap-to-claim buttons and
rewrites itself in place as agents claim slots, so the board is always current
instead of buried under copy-pasted replies.

Setup: follow SETUP.md step by step. README.md is the reference guide.
"""

from __future__ import annotations

"""Settings, database, and everything shared between the modules."""

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
    KeyboardButton,
    ReplyKeyboardMarkup,
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


DAY_ALIASES = {
    "THURS": "THU", "THURSDAY": "THU", "THUR": "THU",
    "MONDAY": "MON", "TUESDAY": "TUE", "TUES": "TUE", "WEDNESDAY": "WED",
    "WEDS": "WED", "FRIDAY": "FRI", "SATURDAY": "SAT", "SUNDAY": "SUN",
}


def norm_day(text: str) -> str | None:
    """Accept THU, THURS, Thursday — all mean the same day."""
    d = text.strip().upper()
    d = DAY_ALIASES.get(d, d)
    return d if d in DAY_NAMES else None


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
# Time of day the avails reminders go out, so nobody gets pinged at midnight.
NUDGE_TIME = os.environ.get("NUDGE_TIME", "").strip() or "11:59"
# Suggested deadline time when you post a week. 23:59 means the closing notice
# lands at midnight, so a daytime value keeps every message in waking hours.
DEADLINE_TIME = os.environ.get("DEADLINE_TIME", "").strip() or "23:59"
# Final call on the deadline day, well before it closes.
LAST_CALL_TIME = os.environ.get("LAST_CALL_TIME", "").strip() or "20:00"
# Don't announce a closure in the middle of the night — the board still closes,
# it just doesn't ping anyone until morning.
QUIET_FROM = env_int("QUIET_FROM", 22)
QUIET_UNTIL = env_int("QUIET_UNTIL", 8)
# Personal DMs the evening before. Off by default so nobody is pinged twice.
DM_REMINDERS = os.environ.get("DM_REMINDERS", "").strip().lower() in {"1", "true", "yes", "on"}

# Your team numbers weeks one behind ISO: the Monday of 10 Aug 2026 is ISO week
# 33, but you label it W32. Set to 0 if you ever switch to plain ISO numbering.
WEEK_NUM_OFFSET = env_int("WEEK_NUM_OFFSET", -1)
# Fallback hourly rate in cents, used until you set one with /setrate.
DEFAULT_RATE_CENTS = env_int("DEFAULT_RATE_CENTS", 0)
# Paid per Google review credited to an agent.
REVIEW_RATE_CENTS = env_int("REVIEW_RATE_CENTS", 1000)
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
# Overtime past the end of a rostered block. Anything under OT_MIN_MINUTES is
# treated as packing up rather than work, so a 12:02 clock-out isn't paid as OT.
OT_MIN_MINUTES = env_int("OT_MIN_MINUTES", 5)
# Above this, admins get told — usually a forgotten clock-out rather than real OT.
OT_ALERT_MINUTES = env_int("OT_ALERT_MINUTES", 60)
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
# Where the daily on-duty tags go. Defaults to the avails group; set it to the
# TC Online id to send them there instead. "ops" is shorthand for OPS_CHAT_ID.
_sc_raw = os.environ.get("SHIFTCALL_CHAT_ID", "").strip().lower()
if _sc_raw == "ops":
    SHIFTCALL_CHAT_ID = OPS_CHAT_ID or GROUP_CHAT_ID
else:
    SHIFTCALL_CHAT_ID = env_int("SHIFTCALL_CHAT_ID") or GROUP_CHAT_ID
# Topic within that chat. Only inherits the board's topic if it's the same chat.
SHIFTCALL_THREAD_ID = env_int("SHIFTCALL_THREAD_ID") or (
    OPS_THREAD_ID if SHIFTCALL_CHAT_ID == OPS_CHAT_ID and OPS_CHAT_ID
    else (GROUP_THREAD_ID if SHIFTCALL_CHAT_ID == GROUP_CHAT_ID else None)
)

# "single" = one pinned message holding the whole week (neater).
# "daily"  = one message per day (buttons sit closer to their day).
BOARD_MODE = (os.environ.get("BOARD_MODE", "").strip().lower() or "single")
# "on" keeps tap-to-claim buttons on the group board. "off" makes the group
# board a clean read-only summary and moves all claiming into /plan.
GROUP_BUTTONS = (os.environ.get("GROUP_BUTTONS", "").strip().lower() or "on") != "off"
# Short how-to posted under a new board. Set to "off" once the team knows the drill.
POST_INTRO = (os.environ.get("POST_INTRO", "").strip().lower() or "on") != "off"

DAY_NAMES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
DAY_ABBR = {"MON": "M", "TUE": "T", "WED": "W", "THU": "Th",
            "FRI": "F", "SAT": "Sa", "SUN": "Su"}

# Your standard pattern: 5 blocks weekdays, 4 on the weekend.
DEFAULT_SLOTS = {
    "MON": ["10am-12pm", "12pm-2pm", "2pm-4pm", "4pm-6pm", "6pm-8pm"],
    "TUE": ["10am-12pm", "12pm-2pm", "2pm-4pm", "4pm-6pm", "6pm-8pm"],
    "WED": ["10am-12pm", "12pm-2pm", "2pm-4pm", "4pm-6pm", "6pm-8pm"],
    "THU": ["10am-12pm", "12pm-2pm", "2pm-4pm", "4pm-6pm", "6pm-8pm"],
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
    start_min  INTEGER NOT NULL,
    capacity   INTEGER
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
CREATE TABLE IF NOT EXISTS fixed_slots (
    user_id  INTEGER NOT NULL,
    day_name TEXT NOT NULL,
    label    TEXT NOT NULL,
    PRIMARY KEY (user_id, day_name, label)
);
CREATE TABLE IF NOT EXISTS drop_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL DEFAULT 'drop',
    agent_id     INTEGER NOT NULL,
    slot_id      INTEGER NOT NULL,
    reason       TEXT,
    target_id    INTEGER,
    requested_at TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    decided_by   INTEGER,
    decided_at   TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   INTEGER NOT NULL,
    the_date   TEXT NOT NULL,
    photo_id   TEXT,
    note       TEXT,
    added_by   INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ho_cases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   INTEGER NOT NULL,
    the_date   TEXT NOT NULL,
    section    TEXT NOT NULL DEFAULT 'open',
    prio       TEXT,
    platform   TEXT,
    flag       TEXT,
    store      TEXT,
    username   TEXT,
    body       TEXT,
    closed     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    closed_by  INTEGER,
    closed_at  TEXT
);
CREATE TABLE IF NOT EXISTS handovers (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id  INTEGER NOT NULL,
    the_date  TEXT NOT NULL,
    body      TEXT,
    created_at TEXT NOT NULL
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
    note       TEXT,
    shift_label TEXT,
    opening_posted INTEGER NOT NULL DEFAULT 0
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
# The web server runs on its own thread and shares this connection. Without
# these two settings a read from the web thread can sit waiting behind a write
# on the bot's thread, which shows up as the Mini App spinning forever.
try:
    db.execute("PRAGMA journal_mode=WAL")      # readers don't block on writers
    db.execute("PRAGMA busy_timeout=8000")     # wait 8s, then fail loudly
    db.commit()
except Exception as _e:                        # a volume that can't do WAL
    log.warning("Couldn't set WAL mode: %s", _e)
db.executescript(SCHEMA)
# Days used to be stored as THURS. Bring old rows in line with the new THU.
for _tbl, _col in (("days", "name"), ("fixed_slots", "day_name")):
    try:
        db.execute(f"UPDATE {_tbl} SET {_col}='THU' WHERE {_col}='THURS'")
    except sqlite3.OperationalError:
        pass
try:
    for _p in db.execute("SELECT name, config FROM presets").fetchall():
        if "THURS" in _p[1]:
            db.execute(
                "UPDATE presets SET config=? WHERE name=?",
                (_p[1].replace('"THURS"', '"THU"'), _p[0]),
            )
except sqlite3.OperationalError:
    pass
db.commit()

_slot_cols = {r[1] for r in db.execute("PRAGMA table_info(slots)")}
if "capacity" not in _slot_cols:
    db.execute("ALTER TABLE slots ADD COLUMN capacity INTEGER")
_dr_cols = {r[1] for r in db.execute("PRAGMA table_info(drop_requests)")}
if "kind" not in _dr_cols:
    db.execute("ALTER TABLE drop_requests ADD COLUMN kind TEXT NOT NULL DEFAULT 'drop'")
if "notified" not in _dr_cols:
    db.execute("ALTER TABLE drop_requests ADD COLUMN notified TEXT")
if "target_id" not in _dr_cols:
    db.execute("ALTER TABLE drop_requests ADD COLUMN target_id INTEGER")
_te_cols = {r[1] for r in db.execute("PRAGMA table_info(time_entries)")}
for _c, _d in (("shift_label", "TEXT"),
               ("opening_posted", "INTEGER NOT NULL DEFAULT 0")):
    if _c not in _te_cols:
        db.execute(f"ALTER TABLE time_entries ADD COLUMN {_c} {_d}")
_cols = {r[1] for r in db.execute("PRAGMA table_info(agents)")}
if "role" not in _cols:
    db.execute(
        "ALTER TABLE agents ADD COLUMN role TEXT NOT NULL DEFAULT 'agent'"
    )
for _col, _ddl in [
    ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ("display_name", "TEXT"),
    ("req_msgs", "TEXT"),
    ("support_name", "TEXT"),
    ("on_avails", "INTEGER NOT NULL DEFAULT 1"),
    ("tag_calls", "INTEGER NOT NULL DEFAULT 1"),
    ("salaried", "INTEGER NOT NULL DEFAULT 0"),
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


TG_LIMIT = 3900  # Telegram caps a message at 4096; leave room for entities


def split_message(text: str, limit: int = TG_LIMIT) -> list:
    """Break a long report on line boundaries so nothing is lost."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(line) > limit:                      # one enormous line
            if cur:
                parts.append(cur); cur = ""
            for i in range(0, len(line), limit):
                parts.append(line[i:i + limit])
            continue
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


async def reply_long(message, text: str, **kw):
    """reply_text, but split into several messages when it's too long."""
    chunks = split_message(text)
    last = None
    for i, chunk in enumerate(chunks):
        tail = dict(kw)
        if i < len(chunks) - 1:
            tail.pop("reply_markup", None)          # markup only on the last one
        last = await message.reply_text(chunk, **tail)
    return last


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


async def notify_admins(bot, text: str, kb=None) -> list:
    """Message every admin, and remember where, so a decision can update them all."""
    sent = []
    for aid in admin_ids():
        try:
            m = await bot.send_message(
                aid, text, reply_markup=kb, parse_mode=constants.ParseMode.HTML
            )
            sent.append([aid, m.message_id])
        except Exception as e:
            log.info("Couldn't notify admin %s: %s", aid, e)
    return sent


async def settle_admin_messages(bot, sent, text: str) -> None:
    """Replace the buttons everywhere with the outcome."""
    for chat_id, msg_id in sent or []:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=text,
                parse_mode=constants.ParseMode.HTML,
            )
        except Exception:
            pass


async def send_group(bot, text: str, thread: int | None = -1,
                     chat_id: int | None = None, **kw):
    """Post to a group, into the right forum topic if one is configured."""
    tid = GROUP_THREAD_ID if thread == -1 else thread
    if tid:
        kw["message_thread_id"] = tid
    return await bot.send_message(chat_id or GROUP_CHAT_ID, text, **kw)


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


def week_number_start(n: int, year: int | None = None) -> date | None:
    """Monday of the week your team calls W<n>.

    Your labels run one behind ISO (WEEK_NUM_OFFSET), so undo that first.
    With no year given, pick the most recent W<n> that has actually started —
    so asking for W52 in January means last December, not eleven months away.
    """
    iso = n - WEEK_NUM_OFFSET
    if not 1 <= iso <= 53:
        return None

    def monday(y):
        try:
            return date.fromisocalendar(y, iso, 1)
        except ValueError:
            return None

    if year:
        return monday(year)

    today = now().date()
    this_year = monday(today.year)
    if this_year and this_year <= today:
        return this_year
    return monday(today.year - 1) or this_year


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


def is_salaried(agent_id: int) -> bool:
    """Full-timers on a salary — rostered and clocked like anyone else, but
    their livechat hours don't accrue hourly pay."""
    row = q1("SELECT salaried FROM agents WHERE user_id=?", (agent_id,))
    return bool(row and row["salaried"])


def rate_for(agent_id: int, on: date) -> int:
    """Hourly rate in cents that applied on a given day."""
    if is_salaried(agent_id):
        return 0
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


def entry_split(row) -> tuple:
    """(rostered minutes, overtime minutes) for one entry.

    The rostered block is the floor — clocking in a minute late doesn't cost
    anyone. Staying past the end is paid on top, to the minute.
    """
    if not row["clock_out"]:
        return 0, 0
    actual = entry_minutes(row)
    if PAY_MODE != "slot" or not row["slot_id"]:
        return actual, 0

    out = datetime.fromisoformat(row["clock_out"])
    day = date.fromisoformat(row["the_date"])
    midnight = datetime.combine(day, time(0, 0), TZ)

    base, last_end = 0, None
    for sl in slot_run_from(row["slot_id"]):
        end_min = sl["start_min"] + slot_minutes(sl["label"])
        slot_end = midnight + timedelta(minutes=end_min)
        if out >= slot_end - timedelta(minutes=SLOT_GRACE_MIN):
            base += slot_minutes(sl["label"])
            last_end = slot_end
    if not base:
        return actual, 0

    ot = 0
    if last_end and out > last_end:
        over = int((out - last_end).total_seconds() // 60)
        if over >= OT_MIN_MINUTES:
            ot = over
    return base, ot


def entry_paid_minutes(row) -> int:
    """Total minutes an entry is paid for, rostered plus any overtime."""
    base, ot = entry_split(row)
    return base + ot


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
    # A rostered block is paid once, however many times someone clocked into it.
    credited: set = set()
    for r in rows:
        if not r["clock_out"]:
            open_count += 1
            continue
        base, ot = entry_split(r)
        m = base + ot
        actual = entry_minutes(r)
        day = date.fromisoformat(r["the_date"])
        dup = False
        if PAY_MODE == "slot" and r["slot_id"]:
            key = (r["the_date"], r["slot_id"])
            if key in credited:
                m, base, ot, dup = 0, 0, 0, True
            else:
                credited.add(key)
        pay = round(m / 60 * rate_for(agent_id, day))
        minutes += m
        cents += pay
        shifts.append({
            "row": r, "date": day, "minutes": m, "base": base, "ot": ot,
            "actual": actual, "cents": pay, "duplicate": dup,
        })
    return {
        "shifts": shifts,
        "overtime": sum(sh["ot"] for sh in shifts),
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


BTN_IN = "⏱ Clock in"
BTN_OUT = "✅ Clock out"
BTN_HOURS = "🕐 My hours"
BTN_SHIFTS = "📋 My shifts"


def agent_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """The buttons that sit under the message box, so nothing is typed.

    Shows Clock out while they're on shift, Clock in otherwise.
    """
    on_shift = bool(q1(
        "SELECT 1 FROM time_entries WHERE agent_id=? AND clock_out IS NULL",
        (user_id,),
    ))
    rows = [
        [KeyboardButton(BTN_OUT if on_shift else BTN_IN)],
        [KeyboardButton(BTN_HOURS), KeyboardButton(BTN_SHIFTS)],
    ]
    return ReplyKeyboardMarkup(
        rows, resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Tap a button, or type a command",
    )


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


def week_containing(d: date) -> sqlite3.Row | None:
    """The week whose dates cover this day, whatever its status."""
    return q1(
        "SELECT * FROM weeks WHERE ? BETWEEN start_date AND end_date "
        "ORDER BY id DESC LIMIT 1",
        (d.isoformat(),),
    )


def this_week() -> sqlite3.Row | None:
    """The week being worked right now.

    Different from latest_week(): once next week opens for avails, that one is
    'latest', but people still need to see the week they're actually in.
    """
    return week_containing(now().date()) or latest_week()


def latest_week() -> sqlite3.Row | None:
    """The week we're actually in.

    Prefer the open one, then whichever covers today, then the one starting
    soonest, and only fall back to newest-created. Picking purely by id means a
    stray test week posted later hides the week everyone is working.
    """
    today = now().date().isoformat()
    row = q1("SELECT * FROM weeks WHERE status='open' ORDER BY id DESC LIMIT 1")
    if row:
        return row
    row = q1(
        "SELECT * FROM weeks WHERE ? BETWEEN start_date AND end_date "
        "ORDER BY id DESC LIMIT 1",
        (today,),
    )
    if row:
        return row
    row = q1(
        "SELECT * FROM weeks WHERE start_date >= ? ORDER BY start_date LIMIT 1",
        (today,),
    )
    if row:
        return row
    return q1("SELECT * FROM weeks ORDER BY end_date DESC, id DESC LIMIT 1")


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
        cap = s["capacity"] or SLOT_CAPACITY
        filled = ", ".join(names)
        if cap > 1 and names:
            filled += f"  ({len(names)}/{cap})"
        elif cap > 1:
            filled = f"(0/{cap})"
        lines.append(f"{s['label']}: {esc(filled)}")
        if len(names) >= cap:
            tag = " ✓"
        elif names:
            tag = f" ({len(names)}/{cap})" if cap > 1 else f" ({len(names)})"
        else:
            tag = f" (0/{cap})" if cap > 1 else ""
        buttons.append(
            InlineKeyboardButton(
                short_label(s["label"]) + tag, callback_data=f"t:{s['id']}"
            )
        )

    if closed:
        lines.append("\n<i>Submissions closed.</i>")

    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    return "\n".join(lines), InlineKeyboardMarkup([] if closed else rows)


def apply_fixed_slots(week_id: int) -> int:
    """Put people with a fixed weekly pattern straight onto the new board."""
    filled = 0
    rows = q(
        """SELECT f.user_id, f.day_name, f.label, a.display_name, a.name
           FROM fixed_slots f JOIN agents a ON a.user_id = f.user_id
           WHERE a.status='active'"""
    )
    for r in rows:
        slot = q1(
            """SELECT s.id FROM slots s JOIN days d ON d.id = s.day_id
               WHERE d.week_id=? AND d.name=? AND lower(s.label)=lower(?)""",
            (week_id, r["day_name"], r["label"]),
        )
        if not slot:
            continue
        taken = q1("SELECT 1 FROM signups WHERE slot_id=?", (slot["id"],))
        if taken:
            continue
        cur = run(
            "INSERT OR IGNORE INTO signups (slot_id, user_id, name, ts) VALUES (?,?,?,?)",
            (slot["id"], r["user_id"],
             r["display_name"] or r["name"], now().isoformat()),
        )
        filled += cur.rowcount
    return filled


def week_stats(week_id: int) -> dict:
    roster = q("SELECT * FROM agents WHERE status='active' AND on_avails=1")
    confirmed = {
        r["user_id"] for r in q("SELECT user_id FROM confirmations WHERE week_id=?", (week_id,))
    }
    gaps = q(
        """SELECT d.name, d.the_date, s.label, s.capacity,
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
        "gaps": [
            g for g in gaps
            if g["n"] < (g["capacity"] or MIN_PER_SLOT)
        ],
        "total_slots": len(gaps),
    }


def slot_holders(slot_id: int) -> list[sqlite3.Row]:
    return q(
        "SELECT user_id, name FROM signups WHERE slot_id=? ORDER BY ts", (slot_id,)
    )


def slot_capacity(slot_id: int) -> int:
    """How many agents this particular slot takes."""
    row = q1("SELECT capacity FROM slots WHERE id=?", (slot_id,))
    if row and row["capacity"]:
        return row["capacity"]
    return SLOT_CAPACITY


def slot_taken_by(slot_id: int, user_id: int) -> str | None:
    """Returns the blocking holders' names, or None if the user may claim it."""
    holders = slot_holders(slot_id)
    if any(h["user_id"] == user_id for h in holders):
        return None
    if len(holders) < slot_capacity(slot_id):
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
            cap = s["capacity"] or SLOT_CAPACITY
            if not names:
                shown = "" if cap == 1 else f"(0/{cap})"
            elif compact and len(names) > 3:
                shown = ", ".join(names[:3]) + f" +{len(names) - 3}"
            else:
                shown = ", ".join(names)
                if cap > 1:
                    shown += f"  ({len(names)}/{cap})"
            lines.append(f"{s['label']}: {esc(shown)}")
            if len(names) >= cap:
                tag = " ✓"
            elif names:
                tag = f" ({len(names)}/{cap})" if cap > 1 else f" ({len(names)})"
            else:
                tag = f" (0/{cap})" if cap > 1 else ""
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

ASK_DATE, ASK_LABEL, ASK_EVENTS, ASK_SLOTS, ASK_SHAPE_LABEL, ASK_DAYS, ASK_DEADLINE, CONFIRM = range(8)


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
            if s["id"] not in mine
            and len(slot_holders(s["id"])) < (s["capacity"] or SLOT_CAPACITY)
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
        cap = s["capacity"] or SLOT_CAPACITY
        if s["id"] in mine:
            others = [h for h in holders if h["user_id"] != user_id]
            extra = f", with {esc(others[0]['name'])}" if others else ""
            state, mark, btn = f"claimed by you{extra}", "✅", "✅ {} — yours"
        elif len(holders) >= cap:
            state = "taken by " + esc(", ".join(h["name"] for h in holders))
            mark, btn = "🔒", "🔒 {} — taken"
        elif holders:
            state = f"{len(holders)}/{cap} — " + esc(holders[0]["name"]) + ", room for you"
            mark, btn = "▫️", "▫️ {} — space left"
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


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


ASK_NAME = 100
DROP_PICK, DROP_REASON = 200, 201
PICKUP_PICK, PICKUP_REASON = 210, 211
HANDOVER_TEXT = 220
HO_SECTION, HO_PRIO, HO_PLATFORM, HO_STORE, HO_BODY, HO_MORE = range(221, 227)
REVIEW_PHOTO = 230
RESTORE_FILE, RESTORE_OK = 250, 251
HO_PICK = 231
SWAP_PICK, SWAP_WHO, SWAP_REASON = 240, 241, 242


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


def day_row_for(the_date: str):
    """The day row for a date, from the live week.

    Old closed weeks can cover the same dates, so always prefer the newest —
    otherwise the rota is read out of a week nobody is working any more.
    """
    return q1(
        """SELECT d.* FROM days d JOIN weeks w ON w.id = d.week_id
           WHERE d.the_date=?
           ORDER BY CASE w.status WHEN 'open' THEN 0 ELSE 1 END, w.id DESC
           LIMIT 1""",
        (the_date,),
    )


def current_slot_for(agent_id: int, when: datetime):
    """The rostered slot this clock-in most likely belongs to."""
    mins = when.hour * 60 + when.minute
    day = day_row_for(when.date().isoformat())
    if not day:
        return None
    rows = q(
        """SELECT s.id, s.label, s.start_min, d.name AS day_name, d.the_date
           FROM signups su JOIN slots s ON s.id = su.slot_id
           JOIN days d ON d.id = s.day_id
           WHERE su.user_id=? AND d.id=? ORDER BY s.start_min""",
        (agent_id, day["id"]),
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


# Stores, as they appear in Duoke. Klipsch deliberately left out.
STORES = [
    ("🇸🇬", "SGLAZADASONOS"),
    ("🇸🇬", "SGLAZMARSHALL"),
    ("🇸🇬", "SGLAZBOWERS"),
    ("🇲🇾", "MYLAZSONOS"),
    ("🇲🇾", "MYLAZBOWERS"),
    ("🇸🇬", "[SG]SHPSONOS"),
    ("🇸🇬", "[SG]SGMARSHALL"),
    ("🇸🇬", "[SG]SHPBOWERS"),
    ("🇲🇾", "[MY]SHPSONOS"),
]
PRIORITIES = [("🟢", "Low"), ("🟠", "Medium"), ("🔴", "High")]
PLATFORMS = ["DUOKE", "LIVECHAT"]


def next_working_day(from_date: date) -> date:
    """The next Mon-Fri, for cases the in-office team picks up."""
    d = from_date + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def render_case(c: dict) -> str:
    lines = [f"▫️{c['platform']}", f"{c['prio']} {c['username']}"]
    if c.get("store"):                       # Duoke only — Livechat has no brands
        lines.append(f"{c.get('flag', '')}{c['store']}")
    return "\n".join(lines) + "\n" + c["body"].strip()


def render_handover(cases: list, who: str, the_date: date,
                    closed: list | None = None) -> str:
    out = [
        "⭐️ Live Chat Agent Closing Handover ⭐️",
        "",
        "> I have closed the tickets on my shift: ✅",
    ]
    now_cases = [c for c in cases if c["section"] == "open"]
    later = [c for c in cases if c["section"] == "follow"]

    if now_cases:
        out += ["", f"🔸{the_date.strftime('%-d %b %Y')}, Open Cases", ""]
        out += ["\n\n".join(render_case(c) for c in now_cases)]
    if later:
        nd = next_working_day(the_date)
        out += ["", f"🔹Follow Up on {nd.strftime('%A, %-d %b %Y')}", ""]
        out += ["\n\n".join(render_case(c) for c in later)]
    if not cases:
        out += ["", f"🔸{the_date.strftime('%-d %b %Y')} — no open cases"]
    if closed:
        out += ["", "✔️ Closed this shift"]
        out += [
            f"  {c['prio']} {c['username']}"
            + (f" — {c['store']}" if c.get("store") else f" — {c['platform']}")
            for c in closed
        ]
    out += ["", f"— {who}"]
    return "\n".join(out)


HANDOVER_TEMPLATE = """▫️DUOKE
🔴customer_handle
🇸🇬[SG]STORE NAME
ORDER NUMBER
Product name

• what happened
• what you did
• who you notified

‼️Need Help: what the next agent should pick up"""


def last_open_handover(exclude_agent: int | None = None):
    """The most recent handover that left something open."""
    row = q1(
        "SELECT * FROM handovers WHERE body IS NOT NULL AND body <> '' "
        "ORDER BY created_at DESC LIMIT 1"
    )
    return row


def handover_keyboard(running=None) -> InlineKeyboardMarkup:
    running = open_cases() if running is None else running
    if running:
        n = len(running)
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📝 Update handover ({n} open)", callback_data="ho:add")],
            [InlineKeyboardButton(
                "↩️ All still open, nothing new", callback_data="ho:carry")],
            [InlineKeyboardButton(
                "✅ All cases closed", callback_data="ho:none")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Add handover", callback_data="ho:add")],
        [InlineKeyboardButton("✅ Nothing to handover", callback_data="ho:none")],
    ])


def open_cases() -> list:
    """Cases still live, oldest first."""
    return q("SELECT * FROM ho_cases WHERE closed=0 ORDER BY id")


def case_row_to_dict(r) -> dict:
    return {
        "section": r["section"], "prio": r["prio"], "platform": r["platform"],
        "flag": r["flag"], "store": r["store"], "username": r["username"],
        "body": r["body"],
    }


PICKER_HELP = (
    "<b>Which cases are still open?</b>\n\n"
    "✅ = still open, carries over\n"
    "☑️ = tap to mark it closed\n\n"
    "<i>Anything you untick won't appear again.</i>"
)


def reviews_for(agent_id: int, first: date, last: date) -> list:
    return q(
        "SELECT * FROM reviews WHERE agent_id=? AND the_date BETWEEN ? AND ? "
        "ORDER BY the_date",
        (agent_id, first.isoformat(), last.isoformat()),
    )


async def flag_to_admins(bot, text: str) -> None:
    """Quietly tell the admins something needs a look."""
    for aid in admin_ids():
        try:
            await bot.send_message(aid, text, parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass


def parse_month(args) -> date:
    if args:
        try:
            return datetime.strptime(args[0], "%Y-%m").date()
        except ValueError:
            pass
    return now().date().replace(day=1)


def week_bounds(d: date) -> tuple:
    mon = d - timedelta(days=d.weekday())
    return mon, mon + timedelta(days=6)


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


EXPECTED_TABLES = {
    "agents", "weeks", "days", "slots", "signups", "time_entries",
    "pay_rates", "handovers", "ho_cases", "reviews", "drop_requests",
}


def inspect_db(path: str) -> dict:
    """Read a database file without touching the live one."""
    out = {"ok": False, "tables": set(), "counts": {}, "newest": None}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
    except Exception as e:
        out["error"] = f"Can't open it: {e}"
        return out
    try:
        names = {
            r["name"] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        out["tables"] = names
        missing = EXPECTED_TABLES - names
        if missing:
            out["error"] = "Not an avails database — missing " + ", ".join(
                sorted(missing)[:4]
            )
            return out
        for t in ("agents", "weeks", "time_entries", "signups", "handovers",
                  "reviews"):
            out["counts"][t] = con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        r = con.execute(
            "SELECT MAX(the_date) d FROM time_entries"
        ).fetchone()
        out["newest"] = r["d"] if r else None
        out["ok"] = True
    except Exception as e:
        out["error"] = f"Couldn't read it: {e}"
    finally:
        con.close()
    return out


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

    hour = now().hour
    quiet = (hour >= QUIET_FROM or hour < QUIET_UNTIL)
    if quiet:
        log.info("Week %s closed quietly at %02d:00", week_id, hour)
        return

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


AGENT_COMMANDS = [
    ("clockin", "Start my shift — or /clockin 2pm-4pm"),
    ("clockout", "End my shift"),
    ("mytime", "My hours this month"),
    ("payslip", "My hours and pay"),
    ("dropshift", "Ask to drop a shift"),
    ("pickup", "Ask to take an open shift"),
    ("swap", "Hand a shift to someone"),
    ("support", "Who I list as support"),
    ("handover", "Post a closing handover"),
    ("help", "List commands"),
]
GROUP_COMMANDS = [
    ("schedule", "Show this week's schedule"),
]

# Grouped so /help and the command menu can never drift apart.
ADMIN_GROUPS = [
    ("Running the week", [
        ("newweek", "Set up and post a week"),
        ("schedule", "Move the board to the bottom of the group"),
        ("gaps", "Unfilled slots"),
        ("remind", "Nudge whoever hasn't confirmed"),
        ("closeweek", "Close submissions early"),
        ("shiftcall", "On-duty tags — add 'preview' to test"),
        ("presets", "Saved timing patterns"),
        ("savepreset", "Save this week's timings for reuse"),
    ]),
    ("Slots", [
        ("capacity", "How many agents a slot takes"),
        ("events", "Fix the Key Events lines"),
        ("addslot", "Add a slot to the live week"),
        ("delslot", "Remove a slot from the live week"),
        ("whohas", "Who is on a slot"),
        ("dropslot", "Free one slot from someone"),
        ("fixed", "Slots someone always works"),
        ("applyfixed", "Apply fixed rosters to this week"),
    ]),
    ("People", [
        ("pending", "Approve or decline access requests"),
        ("dropreqs", "Shift requests waiting"),
        ("handovers", "Recent closing handovers"),
        ("access", "Who approved or declined whom"),
        ("roster", "Who's on the list"),
        ("rename", "Fix someone's name on the schedule"),
        ("avails", "Who gets tagged for avails"),
        ("salaried", "Who isn't paid hourly"),
        ("tag", "Who gets pinged in the nightly post"),
        ("removeagent", "Remove someone, frees their slots"),
    ]),
    ("Hours and pay", [
        ("openshifts", "Who's clocked in now"),
        ("clockoutfor", "Close a forgotten shift"),
        ("fixtime", "Correct a time entry"),
        ("week", "The week at a glance — or /week W36"),
        ("timesheet", "Hours — add @handle, week or W37"),
        ("audit", "Rostered vs actually clocked"),
        ("addtime", "Record a missed shift"),
        ("addreview", "Credit a Google review"),
        ("reviews", "Reviews credited this month"),
    ]),
]
OWNER_EXTRA = [
    ("makeadmin", "Give someone admin rights"),
    ("removeadmin", "Take admin rights away"),
    ("setrate", "Set the hourly rate"),
    ("payroll", "Export shifts as CSV"),
    ("export", "This week's board as CSV"),
    ("backup", "Download a copy of the data"),
    ("restore", "Put a backup back"),
    ("dbinfo", "What's in the database"),
    ("chatid", "Show this chat's ID"),
    ("tidy", "Clear old closed weeks only"),
    ("reset", "Clear EVERYTHING except people"),
]
ADMIN_COMMANDS = AGENT_COMMANDS + [
    c for _, group in ADMIN_GROUPS for c in group
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
  body { margin:0; padding:16px 16px 40px;
    font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--tg-theme-bg-color, #fff);
    color: var(--tg-theme-text-color, #111); }
  h1 { font-size:20px; margin:0 0 2px; }
  .sub { color: var(--tg-theme-hint-color,#777); font-size:13px; margin-bottom:18px; }
  .cards { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:22px; }
  .card { background: var(--tg-theme-secondary-bg-color,#f4f4f5);
          border-radius:14px; padding:14px; }
  .card .n { font-size:22px; font-weight:650; letter-spacing:-0.02em; }
  .card .l { font-size:12px; color: var(--tg-theme-hint-color,#777); margin-top:2px; }
  .card.wide { grid-column: 1 / -1; }
  .card.pay .n { color: var(--tg-theme-link-color,#2a7); }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em;
       color: var(--tg-theme-hint-color,#777); margin:0 0 8px; font-weight:600; }
  .row { display:flex; justify-content:space-between; align-items:baseline;
         padding:11px 0; border-bottom:1px solid var(--tg-theme-secondary-bg-color,#eee); }
  .row:last-child { border-bottom:0; }
  .d { font-weight:550; }
  .t { font-size:12px; color: var(--tg-theme-hint-color,#777); margin-top:1px; }
  .h { font-variant-numeric: tabular-nums; text-align:right; }
  .flag { font-size:11px; color:#c60; }
  .empty { text-align:center; padding:40px 20px; color: var(--tg-theme-hint-color,#777); }
  .note { margin-top:22px; font-size:12px; color: var(--tg-theme-hint-color,#777); }
  .err { background:#fee; color:#900; padding:14px; border-radius:12px; }
  .tabs { display:flex; gap:8px; margin-bottom:18px; }
  .tab { flex:1; padding:9px; text-align:center; border-radius:10px; font-size:14px;
         font-weight:600; background: var(--tg-theme-secondary-bg-color,#f4f4f5);
         color: var(--tg-theme-hint-color,#777); cursor:pointer; }
  .tab.on { background: var(--tg-theme-link-color,#2a7); color:#fff; }
  .nav { display:flex; align-items:center; justify-content:space-between;
         margin-bottom:16px; }
  .nav button { background: var(--tg-theme-secondary-bg-color,#f4f4f5);
     border:0; border-radius:9px; padding:7px 14px; font-size:16px;
     color: var(--tg-theme-text-color,#111); cursor:pointer; }
  .nav button[disabled] { opacity:.3; }
  .nav .m { font-weight:650; }
  .p { padding:12px 0; border-bottom:1px solid var(--tg-theme-secondary-bg-color,#eee); }
  .p:last-child { border-bottom:0; }
  .ptop { display:flex; justify-content:space-between; align-items:baseline; }
  .pn { font-weight:600; }
  .pp { font-variant-numeric:tabular-nums; font-weight:600; }
  .track { height:5px; border-radius:3px; margin-top:7px;
           background: var(--tg-theme-secondary-bg-color,#eee); overflow:hidden; }
  .fill { height:100%; background: var(--tg-theme-link-color,#2a7); }
  .warn { background:#fff6e5; color:#8a5a00; padding:11px 13px;
          border-radius:11px; font-size:13px; margin-bottom:16px; }
  body { padding-bottom: 84px; }
  .nav-bar { position:fixed; left:0; right:0; bottom:0; display:flex;
    background: var(--tg-theme-bg-color,#fff);
    border-top:1px solid var(--tg-theme-secondary-bg-color,#eee);
    padding:8px 4px calc(8px + env(safe-area-inset-bottom)); z-index:10; }
  .nav-bar div { flex:1; text-align:center; font-size:11px; padding:4px 0;
    color: var(--tg-theme-hint-color,#888); cursor:pointer; }
  .nav-bar div span { display:block; font-size:19px; line-height:1.3; }
  .nav-bar div.on { color: var(--tg-theme-link-color,#2a7); font-weight:600; }
  .hero { border-radius:18px; padding:20px; margin-bottom:18px;
          background: var(--tg-theme-secondary-bg-color,#f4f4f5); }
  .hero.live { background: var(--tg-theme-link-color,#2a7); color:#fff; }
  .hero .k { font-size:13px; opacity:.75; }
  .hero .v { font-size:26px; font-weight:650; letter-spacing:-.02em; margin-top:2px; }
  .big { width:100%; border:0; border-radius:14px; padding:16px;
         font-size:17px; font-weight:650; cursor:pointer; margin-top:14px;
         background: var(--tg-theme-link-color,#2a7); color:#fff; }
  .big.stop { background:#c0392b; }
  .big[disabled] { opacity:.5; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
  .chips button { border:0; border-radius:10px; padding:9px 14px; font-size:14px;
    background: var(--tg-theme-secondary-bg-color,#f4f4f5);
    color: var(--tg-theme-text-color,#111); cursor:pointer; }
  .day { margin-top:20px; }
  .day h3 { font-size:12px; font-weight:700; letter-spacing:.06em;
     text-transform:uppercase; color: var(--tg-theme-hint-color,#888);
     margin:0 0 8px; display:flex; justify-content:space-between; }
  .day.is-today h3 { color: var(--tg-theme-link-color,#2a7); }
  .slot { display:flex; align-items:center; justify-content:space-between;
     padding:11px 14px; border-radius:11px; margin-bottom:7px; font-size:14px;
     background: var(--tg-theme-secondary-bg-color,#f4f4f5); }
  .slot.tappable { cursor:pointer; -webkit-tap-highlight-color:transparent; }
  .slot.tappable:active { transform:scale(.985); }
  .slot.busy { opacity:.5; }
  .toast { position:fixed; left:16px; right:16px; bottom:96px; z-index:20;
     background:#333; color:#fff; padding:12px 16px; border-radius:11px;
     font-size:13px; text-align:center; }
  .slot .who { font-size:12px; color: var(--tg-theme-hint-color,#888);
     text-align:right; }
  .slot.mine { background: var(--tg-theme-link-color,#2a7); color:#fff; }
  .slot.mine .who { color:#dff1e9; }
  .slot.free { background:transparent;
     border:1px dashed var(--tg-theme-hint-color,#c9c9cf); }
  .slot.free .who { color: var(--tg-theme-link-color,#2a7); }
  .slot.part { background:#fff6e5; }
  .slot.past { opacity:.45; }
  .cap { font-variant-numeric:tabular-nums; }
  .seg { display:inline-flex; gap:2px; padding:3px; border-radius:9px;
         background: var(--tg-theme-secondary-bg-color,#f4f4f5); margin-bottom:14px; }
  .seg div { padding:5px 16px; border-radius:7px; font-size:13px; font-weight:600;
             color: var(--tg-theme-hint-color,#777); cursor:pointer; }
  .seg div.on { background: var(--tg-theme-bg-color,#fff);
                color: var(--tg-theme-text-color,#111); }
</style>
</head><body>
<div id="app"><div class="empty">Loading…</div></div>
<script>
// Show problems on screen — a webview gives us no console to read.
function showFatal(what, detail) {
  const app = document.getElementById('app');
  if (!app) return;
  app.innerHTML = '<div class="err"><b>' + what + '</b><br><br>'
    + '<code style="font-size:11px;word-break:break-word">'
    + String(detail).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))
    + '</code><br><br>Screenshot this and send it on.</div>';
}
window.onerror = (m, src, line, col) =>
  showFatal('Something broke', m + '  (line ' + line + ':' + col + ')');
window.addEventListener('unhandledrejection', e =>
  showFatal('A request failed', e.reason && e.reason.message || e.reason));

const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
let VIEW = 'home', MODE = 'week', START = '', WHICH = 'now', IS_ADMIN = false;
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function navBar() {
  const items = [['home','🏠','Home'], ['week','📅','Week'], ['me','🕐','Hours']];
  if (IS_ADMIN) items.push(['team','👥','Team']);
  return '<div class="nav-bar">'
    + items.map(([v, i, t]) =>
        `<div class="${VIEW === v ? 'on' : ''}" data-v="${v}"><span>${i}</span>${t}</div>`
      ).join('')
    + '</div>';
}

function tabs() { return ''; }

async function post(url, payload) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'X-Init-Data': tg?.initData || '', 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {})
  });
  return r.json();
}

async function getJSON(url) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 12000);
  try {
    const r = await fetch(url, {
      headers: { 'X-Init-Data': tg?.initData || '' },
      signal: ctl.signal,
    });
    if (!r.ok) throw new Error('HTTP ' + r.status + ' from ' + url);
    return await r.json();
  } catch (e) {
    if (e.name === 'AbortError') {
      throw new Error('No answer from the server after 12s (' + url + ')');
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function loadHome() {
  const app = document.getElementById('app');
  const r = { ok: true, json: () => getJSON('/api/home') };
  const d = await r.json();
  IS_ADMIN = !!d.isAdmin;

  let h = `<h1>${esc(d.name)}</h1><div class="sub">${esc(d.today)}</div>`;

  if (d.onShift) {
    h += '<div class="hero live">';
    h += `<div class="k">On shift${d.shiftLabel ? ' · ' + esc(d.shiftLabel) : ''}</div>`;
    h += `<div class="v">since ${esc(d.since)} · ${esc(d.elapsed)}</div></div>`;
    h += '<button class="big stop" id="act">Clock out</button>';
  } else {
    h += '<div class="hero">';
    h += `<div class="k">${d.nextShift ? 'Next shift' : 'No upcoming shift'}</div>`;
    h += `<div class="v">${esc(d.nextShift || '—')}</div></div>`;
    h += '<button class="big" id="act">Clock in</button>';
  }

  h += '<div class="cards" style="margin-top:20px">';
  h += `<div class="card"><div class="n">${esc(d.weekHours)}</div><div class="l">This week</div></div>`;
  h += `<div class="card"><div class="n">${d.weekShifts}</div><div class="l">Shifts</div></div>`;
  h += '</div>';

  if (d.openCases) {
    h += '<h2>Open cases</h2>';
    for (const c of d.caseNames) h += `<div class="p"><div class="pn">${esc(c)}</div></div>`;
    h += `<div class="note">${d.openCases} case(s) still open — they'll carry into your handover.</div>`;
  }

  app.innerHTML = h + navBar();
  wire();
  const btn = document.getElementById('act');
  if (btn) btn.onclick = () => d.onShift ? doClockOut(btn) : doClockIn(btn, d.slotsToday);
}

async function doClockIn(btn, slots) {
  btn.disabled = true; btn.textContent = 'Clocking in…';
  const out = await post('/api/clockin');
  if (out.needLabel && slots && slots.length) {
    const app = document.getElementById('app');
    let h = `<h1>Clocked in at ${esc(out.at)}</h1>`;
    h += '<div class="sub">Which shift is this?</div><div class="chips">';
    for (const s of slots) h += `<button data-s="${esc(s)}">${esc(s)}</button>`;
    h += '</div>';
    app.innerHTML = h + navBar();
    wire();
    document.querySelectorAll('.chips button').forEach(b => {
      b.onclick = async () => { await post('/api/clockin', { label: b.dataset.s }); render(); };
    });
    return;
  }
  render();
}

async function doClockOut(btn) {
  btn.disabled = true; btn.textContent = 'Clocking out…';
  const out = await post('/api/clockout');
  const app = document.getElementById('app');
  if (!out.ok) { render(); return; }
  let h = `<h1>Clocked out</h1><div class="sub">at ${esc(out.at)}</div>`;
  h += '<div class="cards">';
  h += `<div class="card"><div class="n">${esc(out.total)}</div><div class="l">Worked</div></div>`;
  if (out.ot) h += `<div class="card"><div class="n">+${esc(out.ot)}</div><div class="l">Overtime</div></div>`;
  if (out.pay) h += `<div class="card pay wide"><div class="n">${esc(out.pay)}</div><div class="l">This shift</div></div>`;
  h += '</div>';
  h += '<div class="note">Do your handover in Telegram — the bot will ask.</div>';
  app.innerHTML = h + navBar();
  wire();
}

function wire() {
  document.querySelectorAll('.nav-bar div').forEach(el => {
    el.onclick = () => { VIEW = el.dataset.v; START = ''; render(); };
  });
  document.querySelectorAll('.tab').forEach(el => {
    el.onclick = () => { VIEW = el.dataset.v; START = ''; render(); };
  });
  const p = document.getElementById('prev'), n = document.getElementById('next');
  if (p) p.onclick = () => { START = p.dataset.m; render(); };
  if (n) n.onclick = () => { START = n.dataset.m; render(); };
  document.querySelectorAll('.seg div').forEach(el => {
    el.onclick = () => { MODE = el.dataset.p; START = ''; render(); };
  });
}

async function loadWeek() {
  const app = document.getElementById('app');
  const r = await fetch('/api/week?which=' + WHICH,
    { headers: { 'X-Init-Data': tg?.initData || '' } });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const d = await r.json();

  if (d.empty) {
    app.innerHTML = '<h1>Week</h1><div class="empty">No week has been posted yet.</div>' + navBar();
    wire(); return;
  }

  let h = `<h1>${esc(d.label)}</h1>`;
  const bits = [];
  if (d.mine) bits.push(d.mine + ' shift' + (d.mine === 1 ? '' : 's') + ' yours');
  if (d.open && d.deadline) bits.push('closes ' + esc(d.deadline));
  else if (!d.open) bits.push('closed');
  h += `<div class="sub">${bits.join(' · ')}</div>`;

  if (d.other) {
    h += '<div class="seg" style="margin-top:14px">'
      + `<div data-w="now" class="${WHICH === 'now' ? 'on' : ''}">This week</div>`
      + `<div data-w="next" class="${WHICH === 'next' ? 'on' : ''}">${esc(d.otherLabel)}</div>`
      + '</div>';
  }

  for (const day of d.days) {
    h += `<div class="day${day.today ? ' is-today' : ''}">`;
    h += `<h3><span>${esc(day.name)} ${esc(day.date)}${day.today ? ' · today' : ''}</span></h3>`;
    for (const s of day.slots) {
      let cls = 'slot';
      if (s.mine) cls += ' mine';
      else if (!s.taken) cls += ' free';
      else if (!s.full) cls += ' part';
      if (s.past) cls += ' past';
      let who;
      if (s.mine) who = s.names.length > 1 ? 'you +' + (s.names.length - 1) : 'you';
      else if (!s.taken) who = s.past ? 'nobody' : 'free';
      else who = esc(s.names.join(', '));
      const cap = s.cap > 1 ? ` <span class="cap">${s.taken}/${s.cap}</span>` : '';
      const can = d.open && !s.past && (s.mine || !s.full);
      if (can) cls += ' tappable';
      const attr = can ? ` data-slot="${s.id}" data-want="${s.mine ? '0' : '1'}"` : '';
      h += `<div class="${cls}"${attr}><div>${esc(s.label)}${cap}</div>`;
      h += `<div class="who">${who}</div></div>`;
    }
    h += '</div>';
  }

  if (d.gaps) {
    h += `<div class="note">${d.gaps} slot(s) still open this week.</div>`;
  }
  if (d.open) {
    h += '<div class="note">Tap a slot to take it. Tap yours again to let it go.</div>';
  } else {
    h += '<div class="note">This week is closed — use /pickup to ask for a shift.</div>';
  }

  app.innerHTML = h + navBar();
  wire();
  document.querySelectorAll('.seg div[data-w]').forEach(el => {
    el.onclick = () => { WHICH = el.dataset.w; render(); };
  });
  document.querySelectorAll('.slot.tappable').forEach(el => {
    el.onclick = () => claim(el);
  });
}

function toast(msg) {
  const old = document.querySelector('.toast');
  if (old) old.remove();
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

async function claim(el) {
  if (el.classList.contains('busy')) return;
  el.classList.add('busy');
  const want = el.dataset.want === '1';
  try {
    const out = await post('/api/claim', { slot: Number(el.dataset.slot), want });
    if (!out.ok) {
      toast(out.error || 'Could not change that.');
      await loadWeek();
      return;
    }
    if (tg?.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
    if (!out.quiet && out.label) {
      toast(want ? "You're on " + out.label : "Released " + out.label);
    }
    await loadWeek();
  } catch (e) {
    toast('Something went wrong.');
    el.classList.remove('busy');
  }
}

async function loadTeam() {
  const app = document.getElementById('app');
  const qs = '?mode=' + MODE + (START ? '&start=' + START : '');
  const r = await fetch('/api/team' + qs, {
    headers: { 'X-Init-Data': tg?.initData || '' }
  });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const d = await r.json();

  let h = '<div class="seg">'
    + `<div data-p="week" class="${MODE === 'week' ? 'on' : ''}">Week</div>`
    + `<div data-p="month" class="${MODE === 'month' ? 'on' : ''}">Month</div>`
    + '</div>';
  h += '<div class="nav">'
    + `<button id="prev" data-m="${d.prev}">‹</button>`
    + `<div class="m">${esc(d.label)}</div>`
    + `<button id="next" data-m="${d.next}" ${d.hasNext ? '' : 'disabled'}>›</button>`
    + '</div>';

  h += '<div class="cards">';
  h += `<div class="card"><div class="n">${esc(d.totalHours)}</div><div class="l">Team hours</div></div>`;
  h += `<div class="card"><div class="n">${d.headcount}</div><div class="l">Worked</div></div>`;
  h += `<div class="card pay wide"><div class="n">${esc(d.totalPay)}</div>`;
  h += `<div class="l">${d.totalReviews} review(s)${d.totalOt ? ' · ' + esc(d.totalOt) + ' OT' : ''}</div></div>`;
  h += '</div>';

  if (d.flags) {
    h += `<div class="warn">⚠️ ${d.flags} shift(s) auto-closed or unrostered — check /week</div>`;
  }

  if (d.people.length) {
    h += '<h2>By agent</h2>';
    for (const p of d.people) {
      h += '<div class="p"><div class="ptop">';
      h += `<div class="pn">${esc(p.name)}${p.flags ? ' <span class="flag">⚠️</span>' : ''}</div>`;
      h += `<div class="pp">${esc(p.pay)}</div></div>`;
      h += `<div class="t">${p.shifts} shift(s) · ${esc(p.hours)}`;
      h += p.ot ? ` · +${esc(p.ot)} OT` : '';
      h += p.reviews ? ` · ${p.reviews}⭐` : '';
      h += '</div>';
      h += `<div class="track"><div class="fill" style="width:${p.bar}%"></div></div>`;
      h += '</div>';
    }
  } else {
    h += `<div class="empty">Nothing logged this ${MODE}.</div>`;
  }
  app.innerHTML = h + navBar();
  wire();
}

async function render() {
  if (VIEW === 'home') {
    try { await loadHome(); }
    catch (e) { showFatal('Home would not load', e.message || e); }
    return;
  }
  if (VIEW === 'week') {
    try { await loadWeek(); }
    catch (e) { showFatal('The week would not load', e.message || e); }
    return;
  }
  if (VIEW === 'team') {
    try { await loadTeam(); }
    catch (e) { showFatal('The team view would not load', e.message || e); }
    return;
  }
  await load();
}

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
    IS_ADMIN = !!d.isAdmin;

    let h = `<h1>${esc(d.name)}</h1><div class="sub">${esc(d.month)}</div>`;
    h += '<div class="cards">';
    h += `<div class="card"><div class="n">${d.count}</div><div class="l">Shifts</div></div>`;
    h += `<div class="card"><div class="n">${esc(d.hours)}</div><div class="l">Hours</div></div>`;
    if (d.showPay) {
      h += `<div class="card pay wide"><div class="n">${esc(d.total)}</div>`;
      h += `<div class="l">at ${esc(d.rate)}/hour</div></div>`;
    } else if (d.salaried) {
      h += `<div class="card wide"><div class="l">Salaried — hours are recorded but not paid hourly</div></div>`;
    }
    h += '</div>';
    if (d.shifts.length) {
      h += '<h2>Shifts</h2>';
      for (const s of d.shifts) {
        h += '<div class="row"><div>';
        h += `<div class="d">${esc(s.date)}</div>`;
        h += `<div class="t">${esc(s.times)}${s.ot ? ' · +' + esc(s.ot) + ' OT' : ''}${s.flagged ? ' <span class="flag">· auto-closed</span>' : ''}</div>`;
        h += `</div><div class="h"><div>${esc(s.hours)}</div>`;
        h += s.pay ? `<div class="t">${esc(s.pay)}</div>` : '';
        h += `</div></div>`;
      }
    } else {
      h += '<div class="empty">No shifts logged this month yet.<br>Use /clockin when you start.</div>';
    }
    if (d.openShift) {
      h += '<div class="note">⏱ You are clocked in right now — this shift is not counted yet.</div>';
    }
    app.innerHTML = h + navBar();
    wire();
  } catch (e) {
    const why = e.name === 'AbortError' ? 'The server did not answer in time.' : esc(e.message || e);
    app.innerHTML = '<div class="err">Could not load your hours.<br><br>' + why + '</div>';
  }
}
render();
</script>
</body></html>"""


async def refresh_group_for(week_id: int, day_id: int) -> None:
    """Redraw the pinned board after a change made in the app."""
    bot = bot_ref()
    if not bot:
        return
    try:
        if BOARD_MODE == "single":
            await refresh_board(bot, week_id)
        else:
            await refresh_day(bot, day_id)
            await refresh_header(bot, week_id)
    except Exception as e:
        log.info("Board refresh after an app change failed: %s", e)


# The web server runs on its own thread. Telegram work has to be handed back to
# the bot's event loop. Held in a dict rather than plain globals so every module
# sees the same handle — `global` only rebinds within one module.
_BOT_HANDLE: dict = {"loop": None, "bot": None}


def set_bot_handle(app) -> None:
    try:
        _BOT_HANDLE["loop"] = asyncio.get_running_loop()
        _BOT_HANDLE["bot"] = app.bot
    except RuntimeError:
        pass


def bot_ref():
    return _BOT_HANDLE["bot"]


def on_bot_loop(coro, timeout: float = 8.0):
    """Run a bot coroutine from the web thread and wait for it."""
    loop = _BOT_HANDLE["loop"]
    if loop is None:
        log.warning("No bot loop yet — skipping a call from the app")
        coro.close()
        return None
    try:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
    except Exception as e:
        log.warning("Bot call from the app failed: %s", e)
        return None
