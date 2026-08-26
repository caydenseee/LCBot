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
import html
import io
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
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

# "single" = one pinned message holding the whole week (neater).
# "daily"  = one message per day (buttons sit closer to their day).
BOARD_MODE = (os.environ.get("BOARD_MODE", "").strip().lower() or "single")
# "on" keeps tap-to-claim buttons on the group board. "off" makes the group
# board a clean read-only summary and moves all claiming into /plan.
GROUP_BUTTONS = (os.environ.get("GROUP_BUTTONS", "").strip().lower() or "on") != "off"

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
    role     TEXT NOT NULL DEFAULT 'agent'
);
CREATE TABLE IF NOT EXISTS presets (
    name   TEXT PRIMARY KEY,
    config TEXT NOT NULL
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
            "UPDATE agents SET name=?, username=?, dm_ok=COALESCE(?, dm_ok) WHERE user_id=?",
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
    roster = q("SELECT * FROM agents WHERE active=1")
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
    header = await bot.send_message(
        GROUP_CHAT_ID, text, reply_markup=kb, parse_mode=constants.ParseMode.HTML
    )
    run("UPDATE weeks SET header_msg_id=? WHERE id=?", (header.message_id, week_id))
    try:
        await bot.pin_chat_message(GROUP_CHAT_ID, header.message_id, disable_notification=True)
    except (BadRequest, Forbidden):
        log.info("Couldn't pin — bot may not be a group admin.")

    if BOARD_MODE != "single":
        for d in q("SELECT * FROM days WHERE week_id=? ORDER BY idx", (week_id,)):
            day_text, day_kb = render_day(d["id"])
            msg = await bot.send_message(
                GROUP_CHAT_ID, day_text, reply_markup=day_kb,
                parse_mode=constants.ParseMode.HTML,
            )
            run("UPDATE days SET msg_id=? WHERE id=?", (msg.message_id, d["id"]))

    intro = [
        "☝️ <b>Two ways to fill this in — whichever suits you.</b>",
        "",
        "📱 <b>Easiest:</b> DM me <code>/plan</code> and do your whole week in one "
        "message, without scrolling. There's a <i>Same as last week</i> button there "
        "to start from what you actually did last week, then change whatever's different.",
        "",
        "Or just tap the buttons above, day by day.",
        "",
        "Either way, finish with <b>✅ Slots</b> on the pinned message.",
    ]
    await bot.send_message(
        GROUP_CHAT_ID, "\n".join(intro), parse_mode=constants.ParseMode.HTML
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
                (slot_id, user.id, user.full_name, now().isoformat()),
            )
            note = f"✅ {row['day_name']} {row['label']}"

    await query.answer(note)
    await refresh_group(context, row["week_id"], row["day_id"])


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
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
        free = sum(1 for s in slots if not slot_holders(s["id"]))
        lines.append(
            f"<b>{d['name']}</b>: {', '.join(picked) if picked else '—'}"
        )
        tag = f" · {len(picked)}" if picked else (f" · {free} free" if free else " · full")
        rows.append(
            [InlineKeyboardButton(f"{d['name']}{tag}", callback_data=f"pd:{d['id']}")]
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
            state, mark = "you", "✅"
        elif len(holders) >= SLOT_CAPACITY:
            state, mark = esc(", ".join(h["name"] for h in holders)), "🔒"
        else:
            state, mark = "free", "▫️"
        lines.append(f"{mark} {s['label']} — {state}")
        rows.append(
            [InlineKeyboardButton(f"{mark} {s['label']}", callback_data=f"p:{s['id']}")]
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
                (slot_id, user.id, user.full_name, now().isoformat()),
            )
            note = f"✅ {row['day_name']} {row['label']}"

    await query.answer(note)
    await refresh_plan(query, user.id, row["week_id"], row["day_id"])
    await refresh_group(context, row["week_id"], row["day_id"])


async def on_plan_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Move between the day list and a single day."""
    query = update.callback_query
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
                    (target["id"], user.id, user.full_name, now().isoformat()),
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
    await context.bot.send_message(
        GROUP_CHAT_ID, text, parse_mode=constants.ParseMode.HTML
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
    if chat.type in (constants.ChatType.GROUP, constants.ChatType.SUPERGROUP):
        lines += ["", f"→ <code>GROUP_CHAT_ID={chat.id}</code>"]
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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch_agent(update.effective_user, dm_ok=1)
    await update.message.reply_text(
        "You're set up ✅\n\n"
        "I'll send you a reminder the evening before each of your shifts.\n\n"
        "👉 When a new week is posted, send me /plan — you get your whole week in "
        "one message and can tap it all through without scrolling the group.\n\n"
        "/myshifts — what you're signed up for\n"
        "/summary — the current board"
    )


async def cmd_myshifts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly for that 🙂")
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
    msg = await bot.send_message(
        GROUP_CHAT_ID, text, reply_markup=kb, parse_mode=constants.ParseMode.HTML
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
    rows = q("SELECT * FROM agents WHERE active=1 ORDER BY name")
    if not rows:
        await update.message.reply_text(
            "Roster is empty. Agents join it by tapping a slot, or by sending me /start."
        )
        return
    lines = [f"<b>Roster ({len(rows)})</b>"] + [
        f"{'🔔' if r['dm_ok'] else '🔕'} {esc(r['name'])}"
        + (f" @{r['username']}" if r["username"] else "")
        + f" · <code>{r['user_id']}</code>"
        for r in rows
    ]
    lines.append("\n🔕 = hasn't sent me /start, so can't get shift reminders.")
    lines.append("Remove someone with <code>/removeagent @handle</code> or their ID.")
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
        run("UPDATE agents SET active=0 WHERE user_id=?", (row["user_id"],))
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

    await context.bot.send_message(
        GROUP_CHAT_ID, "\n".join(parts), parse_mode=constants.ParseMode.HTML
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
    await context.bot.send_message(
        GROUP_CHAT_ID, "\n".join(msg), parse_mode=constants.ParseMode.HTML
    )


async def job_close(context: ContextTypes.DEFAULT_TYPE) -> None:
    await close_week(context, context.job.data["week_id"])


def mention(user_id: int, name: str) -> str:
    """@handle if they have one, otherwise a clickable name that still pings."""
    a = q1("SELECT username FROM agents WHERE user_id=?", (user_id,))
    if a and a["username"]:
        return f"@{a['username']}"
    return f'<a href="tg://user?id={user_id}">{esc(name)}</a>'


async def job_shift_call(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Evening-before group post tagging tomorrow's agents."""
    text, reason = build_shift_call()
    if not text:
        log.info("Shift call skipped: %s", reason)
        return
    try:
        await context.bot.send_message(
            GROUP_CHAT_ID, text, parse_mode=constants.ParseMode.HTML
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
    ("export", "Download this week as CSV"),
    ("presets", "Saved timing patterns"),
    ("closeweek", "Close submissions early"),
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
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
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
    app.add_handler(CommandHandler("removeagent", cmd_removeagent))
    app.add_handler(CommandHandler("makeadmin", cmd_makeadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("presets", cmd_presets))
    app.add_handler(CommandHandler("savepreset", cmd_savepreset))
    app.add_handler(CallbackQueryHandler(on_toggle, pattern=r"^t:\d+$"))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^[cu]:\d+$"))
    app.add_handler(CallbackQueryHandler(on_plan_toggle, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(on_plan_nav, pattern=r"^pd:"))
    app.add_handler(CallbackQueryHandler(on_plan_quick, pattern=r"^pq:"))

    log.info("Avails bot running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
