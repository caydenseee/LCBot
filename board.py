"""Setting up and posting the weekly schedule.

Part of the LC avails bot. Shared helpers live in core.py.
"""

from core import *  # noqa: F401,F403

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
    await update.message.reply_text(
        "<b>4/5 — What kind of week is it?</b>",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=week_shape_keyboard(),
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
        # A line starting with + adds to the standard pattern for that day.
        # Anything else replaces that day outright.
        additions: dict[str, list[str]] = {}
        replacements: dict[str, list[str]] = {}
        for raw in txt.splitlines():
            line = raw.strip()
            if not line:
                continue
            is_add = line.startswith("+")
            line = line.lstrip("+").strip()
            if ":" not in line:
                await update.message.reply_text(
                    f"Couldn't read this line:\n<code>{raw}</code>\n"
                    "It needs to look like <code>MON: 10am-12pm, 12pm-2pm</code>\n"
                    "or <code>+FRI: 10pm-12am</code> to add to the standard day.",
                    parse_mode=constants.ParseMode.HTML,
                )
                return ASK_SLOTS
            day_raw, rest = line.split(":", 1)
            day = norm_day(day_raw)
            if not day:
                await update.message.reply_text(
                    f"Unknown day {day_raw.strip()!r}. Use: {', '.join(DAY_NAMES)}"
                )
                return ASK_SLOTS
            labels = [x.strip() for x in rest.split(",") if x.strip()]
            try:
                for lbl in labels:
                    slot_start_minutes(lbl)
            except ValueError as e:
                await update.message.reply_text(f"{e}\nTry again for that day.")
                return ASK_SLOTS
            (additions if is_add else replacements)[day] = labels

        if not additions and not replacements:
            await update.message.reply_text("No days found — try again.")
            return ASK_SLOTS

        if additions:
            cfg = {d: list(v) for d, v in DEFAULT_SLOTS.items()}
            cfg.update(replacements)
            for day, labels in additions.items():
                base = cfg.get(day, [])
                merged = base + [l for l in labels if l not in base]
                cfg[day] = sorted(merged, key=slot_start_minutes)
            draft["slots"] = cfg
        else:
            draft["slots"] = replacements

    return await ask_deadline(update.message.reply_text, draft)


async def ask_deadline(send, draft) -> int:
    """Step 5, reached either by typing timings or tapping them."""
    try:
        _dm = parse_time_token(DEADLINE_TIME)
    except ValueError:
        _dm = 23 * 60 + 59
    default_deadline = datetime.combine(
        draft["monday"] - timedelta(days=1), time(_dm // 60, _dm % 60), TZ
    )
    draft["default_deadline"] = default_deadline
    total = sum(len(v) for v in draft["slots"].values())
    await send(
        f"That's <b>{total} slots</b> across {len(draft['slots'])} days.\n\n"
        f"<b>5/5 — Submission deadline?</b>\nSend <code>-</code> for "
        f"<code>{default_deadline.strftime('%Y-%m-%d %H:%M')}</code> "
        f"({default_deadline.strftime('%a')}), or give your own in the same format.",
        parse_mode=constants.ParseMode.HTML,
    )
    return ASK_DEADLINE


def shape_summary(cfg: dict) -> str:
    lines = []
    for d in DAY_NAMES:
        if d not in cfg or not cfg[d]:
            lines.append(f"{d}: —")
            continue
        last = max(cfg[d], key=slot_start_minutes)
        end = last.split("-")[1] if "-" in last else ""
        lines.append(f"{d}: {len(cfg[d])} slots, ends {end}")
    total = sum(len(v) for v in cfg.values())
    return "\n".join(lines) + f"\n\n<b>{total} slots total</b>"


def week_shape_keyboard(has_slots: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📋 Standard week", callback_data="ws:standard")],
        [InlineKeyboardButton("🌙 Campaign", callback_data="ws:campaign")],
        [InlineKeyboardButton("🎌 Public holiday", callback_data="ws:ph")],
    ]
    saved = [
        r["name"] for r in q("SELECT name FROM presets ORDER BY name")
        if r["name"] not in ("standard", "campaign")
    ]
    for nm in saved[:4]:
        rows.append([InlineKeyboardButton(f"⭐ {nm}", callback_data=f"ws:preset:{nm}")])
    rows.append([InlineKeyboardButton("✏️ Type it myself", callback_data="ws:custom")])
    if has_slots:
        rows.append([InlineKeyboardButton("✓ Done — set deadline", callback_data="ws:done")])
    return InlineKeyboardMarkup(rows)


def day_picker_keyboard(chosen: set, mode: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for d in DAY_NAMES:
        mark = "✅" if d in chosen else "▫️"
        row.append(InlineKeyboardButton(f"{mark} {d}", callback_data=f"wd:{d}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("All days", callback_data="wd:ALL"),
        InlineKeyboardButton("Clear", callback_data="wd:NONE"),
    ])
    label = "Apply campaign" if mode == "campaign" else "Mark holiday"
    rows.append([InlineKeyboardButton(f"✓ {label} & continue", callback_data="wd:DONE")])
    return InlineKeyboardMarkup(rows)


async def on_week_shape(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    draft = context.user_data.get("draft")
    if not draft:
        await query.answer("That setup has expired — send /newweek again.", show_alert=True)
        return ConversationHandler.END
    choice = query.data.split(":", 1)[1]
    await query.answer()

    if choice == "done":
        if not draft.get("slots"):
            draft["slots"] = {d: list(v) for d, v in DEFAULT_SLOTS.items()}
        await query.edit_message_text(
            "<b>Week set up:</b>\n\n" + shape_summary(draft["slots"]),
            parse_mode=constants.ParseMode.HTML,
        )
        return await ask_deadline(query.message.reply_text, draft)

    if choice == "standard":
        draft["slots"] = {d: list(v) for d, v in DEFAULT_SLOTS.items()}
        await query.edit_message_text("📋 Standard week.", parse_mode=None)
        return await ask_deadline(query.message.reply_text, draft)

    if choice.startswith("preset:"):
        name = choice.split(":", 1)[1]
        row = q1("SELECT config FROM presets WHERE name=?", (name,))
        if not row:
            await query.edit_message_text(f"Preset {name} no longer exists.")
            return ASK_SLOTS
        draft["slots"] = json.loads(row["config"])
        await query.edit_message_text(f"⭐ Using preset: {name}", parse_mode=None)
        return await ask_deadline(query.message.reply_text, draft)

    if choice == "custom":
        await query.edit_message_text(
            "Type the days you want to change:\n\n"
            "<code>+FRI: 8pm-10pm, 10pm-12am</code> adds to a standard day\n"
            "<code>MON: 12pm-2pm</code> replaces that day outright",
            parse_mode=constants.ParseMode.HTML,
        )
        return ASK_SLOTS

    if choice == "ph":
        await query.edit_message_text(
            "<b>Which country's holiday?</b>\n\n"
            "Singapore shortens the day. Elsewhere it's business as usual — "
            "we just note it on the board.",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🇸🇬 Singapore — ends 6pm", callback_data="ws:ph_sg")],
                [InlineKeyboardButton("🌏 MY / TH / HK — normal hours",
                                      callback_data="ws:ph_other")],
            ]),
        )
        return ASK_SLOTS

    draft["shape_mode"] = choice
    draft["chosen_days"] = set()
    prompt = {
        "campaign": "What's this campaign called?\n<i>e.g. 9.9, 11.11, Black Friday</i>",
        "ph_sg": "Which holiday is it?\n<i>e.g. National Day, Deepavali</i>",
        "ph_other": "Which holiday is it?\n<i>e.g. Hari Raya (MY), Songkran (TH)</i>",
    }[choice]
    await query.edit_message_text(
        f"<b>4/5 — {prompt}</b>", parse_mode=constants.ParseMode.HTML
    )
    return ASK_SHAPE_LABEL


async def got_shape_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data["draft"]
    label = " ".join(update.message.text.split()).strip()
    if len(label) < 2 or len(label) > 40:
        await update.message.reply_text("Give it a short name, 2 to 40 characters.")
        return ASK_SHAPE_LABEL
    draft["shape_label"] = label
    mode = draft.get("shape_mode", "campaign")
    what = {
        "campaign": f"Which days does <b>{esc(label)}</b> run late? "
                    "(adds 8pm-10pm and 10pm-12am)",
        "ph_sg": f"Which days is <b>{esc(label)}</b>? (those days will end at 6pm)",
        "ph_other": f"Which days is <b>{esc(label)}</b>? (hours stay normal)",
    }[mode]
    await update.message.reply_text(
        f"<b>4/5 — {what}</b>", parse_mode=constants.ParseMode.HTML,
        reply_markup=day_picker_keyboard(set(), mode),
    )
    return ASK_DAYS


async def on_week_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    draft = context.user_data.get("draft")
    if not draft:
        await query.answer("That setup has expired — send /newweek again.", show_alert=True)
        return ConversationHandler.END
    pick = query.data.split(":", 1)[1]
    mode = draft.get("shape_mode", "campaign")
    chosen = draft.setdefault("chosen_days", set())

    if pick == "ALL":
        chosen = set(DAY_NAMES)
    elif pick == "NONE":
        chosen = set()
    elif pick == "DONE":
        if not chosen:
            await query.answer("Pick at least one day.", show_alert=True)
            return ASK_DAYS
        # Build on whatever's already been set, so changes can stack.
        cfg = draft.get("slots") or {d: list(v) for d, v in DEFAULT_SLOTS.items()}
        cfg = {d: list(v) for d, v in cfg.items()}
        for d in chosen:
            base = cfg.get(d, list(DEFAULT_SLOTS.get(d, [])))
            if mode == "campaign":
                for extra in ("8pm-10pm", "10pm-12am"):
                    if extra not in base:
                        base.append(extra)
                cfg[d] = sorted(base, key=slot_start_minutes)
            elif mode == "ph_sg":
                cfg[d] = [l for l in base if slot_start_minutes(l) < 18 * 60]
            else:
                cfg[d] = base          # business as usual elsewhere
        draft["slots"] = cfg
        draft["chosen_days"] = set()

        name = draft.get("shape_label", "")
        days = ", ".join(d for d in DAY_NAMES if d in chosen)
        note = {
            "campaign": f"🌙 {name}: {days}",
            "ph_sg": f"🎌 {name} (SG): {days}",
            "ph_other": f"🎌 {name}: {days}",
        }[mode]
        existing = draft.get("events") or ""
        draft["events"] = (existing + "\n" + note).strip() if existing else note

        await query.answer()
        headline = {
            "campaign": f"✓ {name}: {days}",
            "ph_sg": f"✓ {name}: {days} now end at 6pm",
            "ph_other": f"✓ {name} noted on {days} — hours unchanged",
        }[mode]
        await query.edit_message_text(
            f"{esc(headline)}\n\n{shape_summary(cfg)}\n\n"
            "Anything else to change?",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=week_shape_keyboard(has_slots=True),
        )
        return ASK_SLOTS
    else:
        chosen = chosen ^ {pick}

    draft["chosen_days"] = chosen
    await query.answer()
    try:
        await query.edit_message_reply_markup(
            reply_markup=day_picker_keyboard(chosen, mode)
        )
    except BadRequest:
        pass
    return ASK_DAYS


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

        fixed_filled = apply_fixed_slots(week_id)

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
    msg = "Posted to the group ✅"
    if fixed_filled:
        msg += f"\n{fixed_filled} slot(s) filled from fixed rosters."
    await update.message.reply_text(msg)
    context.user_data.pop("draft", None)
    return ConversationHandler.END


async def cmd_presets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    if not is_admin(update.effective_user.id):
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
    if not is_admin(update.effective_user.id):
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


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/events — fix the Key Events lines on the week that's already posted."""
    if not is_admin(update.effective_user.id):
        return
    w = open_week() or latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return

    current = (w["key_events"] or "").strip()

    if not context.args:
        shown = current or "<i>none</i>"
        await update.message.reply_text(
            f"<b>Key Events — {esc(w['label'])}</b>\n\n{esc(current) if current else shown}"
            "\n\n<code>/events add 🎌 Hari Raya (MY): THU</code>\n"
            "<code>/events del 2</code> — remove line 2\n"
            "<code>/events set ...</code> — replace them all\n"
            "<code>/events clear</code> — remove them all",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    mode = context.args[0].lower()
    rest = " ".join(context.args[1:]).strip()
    lines = [l for l in current.split("\n") if l.strip()]

    if mode == "add":
        if not rest:
            await update.message.reply_text("Give me the line to add.")
            return
        lines.append(rest)
    elif mode == "del":
        if not rest.isdigit() or not 1 <= int(rest) <= len(lines):
            nums = "\n".join(f"{i+1}. {esc(l)}" for i, l in enumerate(lines))
            await update.message.reply_text(
                f"Which line?\n\n{nums}" if lines else "There are no lines yet.",
                parse_mode=constants.ParseMode.HTML,
            )
            return
        lines.pop(int(rest) - 1)
    elif mode == "set":
        if not rest:
            await update.message.reply_text("Give me the text to set.")
            return
        lines = [l for l in rest.split("\\n") if l.strip()]
    elif mode == "clear":
        lines = []
    else:
        # no keyword — treat the whole thing as a replacement
        lines = [l for l in " ".join(context.args).split("\\n") if l.strip()]

    new = "\n".join(lines)
    async with write_lock:
        run("UPDATE weeks SET key_events=? WHERE id=?", (new or None, w["id"]))

    body = esc(new) if new else "<i>none</i>"
    await update.message.reply_text(
        f"✅ <b>Key Events — {esc(w['label'])}</b>\n\n{body}",
        parse_mode=constants.ParseMode.HTML,
    )
    await refresh_group(context, w["id"])


def schedule_week_jobs(app: Application, week_id: int) -> None:
    w = q1("SELECT * FROM weeks WHERE id=?", (week_id,))
    if not w or w["status"] != "open":
        return
    jq = app.job_queue
    dl = datetime.fromisoformat(w["deadline"])

    for name in (f"nudge1-{week_id}", f"nudge2-{week_id}", f"close-{week_id}"):
        for job in jq.get_jobs_by_name(name):
            job.schedule_removal()

    # Both reminders land at a civil hour rather than whenever the offset falls.
    try:
        nm = parse_time_token(NUDGE_TIME)
    except ValueError:
        nm = 11 * 60 + 59
    nudge_at = time(nm // 60, nm % 60)

    try:
        lc = parse_time_token(LAST_CALL_TIME)
    except ValueError:
        lc = 20 * 60
    last_call_at = time(lc // 60, lc % 60)

    plan = [
        (datetime.combine(dl.date() - timedelta(days=2), nudge_at, TZ),
         "Reminder", f"nudge1-{week_id}"),
        (datetime.combine(dl.date(), last_call_at, TZ),
         "Last call", f"nudge2-{week_id}"),
    ]
    for when, prefix, name in plan:
        if now() < when < dl:
            jq.run_once(
                job_nudge, when, name=name, data={"week_id": week_id, "prefix": prefix}
            )
            log.info("%s for week %s scheduled at %s", prefix, week_id, when)
    if dl > now():
        jq.run_once(job_close, dl, name=f"close-{week_id}", data={"week_id": week_id})
