"""Clocking in and out, pay, timesheets and reviews.

Part of the LC avails bot. Shared helpers live in core.py.
"""

from core import *  # noqa: F401,F403

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
        given = " ".join(context.args).strip() if context.args else ""
        if open_row["opening_posted"]:
            shown = open_row["shift_label"] or ""
            await update.message.reply_text(
                f"You're already clocked in since {started.strftime('%-d %b, %H:%M')}"
                + (f" on {shown}" if shown else "")
                + ".\nYour [OPENING] has been posted. Send /clockout when you finish."
            )
            return
        if given:
            # They're mid-shift and now telling me which one it is.
            nice_name = display_name_of(user.id, user.full_name)
            _d = day_row_for(open_row["the_date"])
            match = q1(
                """SELECT s.id FROM slots s JOIN days d ON d.id = s.day_id
                   WHERE d.id=? AND lower(s.label)=lower(?)""",
                (_d["id"], given),
            ) if _d else None
            async with write_lock:
                run(
                    "UPDATE time_entries SET slot_id=COALESCE(?, slot_id), "
                    "shift_label=?, opening_posted=1 WHERE id=?",
                    (match["id"] if match else None, given, open_row["id"]),
                )
            block = opening_block(user.id, nice_name, given)
            posted = await post_ops(context.bot, block)
            if not posted:
                await update.message.reply_text(block, parse_mode=None)
            note = (
                f"⏱ Clocked in since {started.strftime('%H:%M')}\n"
                f"Shift: {esc(given)}\n"
                f"Support: {esc(support_for(user.id, nice_name))}\n\n"
            )
            note += (
                "Your [OPENING] has been posted ✅\n"
                if posted else "Tap the message above to copy it.\n"
            )
            has_roster = day_row_for(open_row["the_date"])
            if not match and has_roster:
                note += "⚠️ That block isn't on today's roster, so it's flagged.\n"
            note += "Send /clockout when you finish."
            await update.message.reply_text(
                note, parse_mode=constants.ParseMode.HTML
            )
            return
        await update.message.reply_text(
            f"You're already clocked in since {started.strftime('%-d %b, %H:%M')}.\n"
            "Send /clockout when you finish."
        )
        return

    override = " ".join(context.args).strip() if context.args else ""
    if override:
        # Covering a shift, or clocking in for a block they aren't rostered on.
        _d = day_row_for(when.date().isoformat())
        slot = q1(
            """SELECT s.id, s.label, s.start_min, d.name AS day_name
               FROM slots s JOIN days d ON d.id = s.day_id
               WHERE d.id=? AND lower(s.label)=lower(?)""",
            (_d["id"], override),
        ) if _d else None
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

    if not slot and not override:
        # Nothing to name the shift after — ask rather than posting a dash.
        entry = q1(
            "SELECT id FROM time_entries WHERE agent_id=? AND clock_out IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (user.id,),
        )
        today_row = day_row_for(when.date().isoformat())
        today_slots = q(
            "SELECT id, label FROM slots WHERE day_id=? ORDER BY idx",
            (today_row["id"],),
        ) if today_row else []
        rows, row = [], []
        for sl in today_slots:
            row.append(
                InlineKeyboardButton(
                    sl["label"], callback_data=f"ci:{entry['id']}:{sl['id']}"
                )
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        if rows:
            await update.message.reply_text(
                f"⏱ Clocked in at <b>{when.strftime('%H:%M')}</b>\n\n"
                "Which shift is this? Tap it and I'll post your [OPENING].\n\n"
                "<i>Next time you can send it straight away — "
                "<code>/clockin 2pm-4pm</code></i>",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(rows),
            )
        else:
            await update.message.reply_text(
                f"⏱ Clocked in at <b>{when.strftime('%H:%M')}</b>\n\n"
                "There's no roster for today, so tell me the shift and I'll post "
                "your [OPENING]:\n\n<code>/clockin 4pm-6pm</code>",
                parse_mode=constants.ParseMode.HTML,
            )
        return

    block = opening_block(user.id, nice_name, slot_text)
    run(
        "UPDATE time_entries SET shift_label=?, opening_posted=1 "
        "WHERE agent_id=? AND clock_out IS NULL",
        (slot_text, user.id),
    )

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
        has_roster = day_row_for(when.date().isoformat())
        note = (
            f"⏱ Clocked in at <b>{when.strftime('%H:%M')}</b>\n"
            f"Shift: {esc(override)}\n"
            f"Support: {esc(support_for(user.id, nice_name))}\n\n"
        )
        if has_roster:
            note += (
                "⚠️ That block isn't on today's roster, so it's logged as "
                "unrostered and your manager will see it flagged.\n\n"
            )
            await flag_to_admins(
                context.bot,
                f"⚠️ <b>Unrostered clock-in</b>\n\n"
                f"<b>{esc(nice_name)}</b> clocked in at "
                f"{when.strftime('%H:%M')} for <b>{esc(override)}</b>, "
                "which isn't on today's roster.\n\n"
                "<code>/openshifts</code> · <code>/fixtime</code>",
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


async def on_clockin_slot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """They picked which shift they've just clocked into."""
    query = update.callback_query
    if not await gate_cb(query):
        return
    _, entry_id, slot_id = query.data.split(":")
    user = query.from_user

    entry = q1("SELECT * FROM time_entries WHERE id=?", (int(entry_id),))
    if not entry or entry["agent_id"] != user.id:
        await query.answer("That clock-in has gone.", show_alert=True)
        return
    if entry["clock_out"]:
        await query.answer("That shift is already closed.", show_alert=True)
        return

    slot = q1(
        """SELECT s.id, s.label, d.name AS day_name FROM slots s
           JOIN days d ON d.id = s.day_id WHERE s.id=?""",
        (int(slot_id),),
    )
    if not slot:
        await query.answer("That slot no longer exists.", show_alert=True)
        return

    async with write_lock:
        run(
            "UPDATE time_entries SET slot_id=?, shift_label=?, opening_posted=1 "
            "WHERE id=?",
            (slot["id"], slot["label"], entry["id"]),
        )

    nice_name = display_name_of(user.id, user.full_name)
    block = opening_block(user.id, nice_name, slot["label"])
    posted = await post_ops(context.bot, block)

    await query.answer(f"{slot['label']} \u2713")
    tail = (
        "Your [OPENING] has been posted \u2705"
        if posted else "Tap the message above to copy it."
    )
    if not posted:
        await query.message.reply_text(block, parse_mode=None)
    await query.edit_message_text(
        f"\u23f1 Clocked in \u2014 <b>{esc(slot['label'])}</b>\n"
        f"Support: {esc(support_for(user.id, nice_name))}\n\n"
        f"{tail}\nSend /clockout when you finish.",
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

    _base, _ot = entry_split(fresh)
    msg = [f"✅ Clocked out at <b>{when.strftime('%H:%M')}</b>"]
    if _ot:
        msg.append(f"Shift: {hhmm(_base)}")
        msg.append(f"Overtime: <b>+{hhmm(_ot)}</b>")
        msg.append(f"Total: <b>{hhmm(mins)}</b>")
    elif PAY_MODE == "slot" and fresh["slot_id"] and mins != actual:
        msg.append(f"Credited: <b>{hhmm(mins)}</b>")
    else:
        msg.append(f"Worked: <b>{hhmm(mins)}</b>")
    if pay:
        msg.append(f"Approx: {money(pay)}")
    msg.append("\n/mytime — your hours this month")
    await update.message.reply_text(
        "\n".join(msg), parse_mode=constants.ParseMode.HTML
    )

    running = open_cases()
    note = ""
    if running:
        note = f"\n\n<i>{len(running)} case(s) still open from earlier shifts.</i>"
    await update.message.reply_text(
        "<b>Closing handover</b>\n\nAnything for the next agent?" + note,
        parse_mode=constants.ParseMode.HTML,
        reply_markup=handover_keyboard(running),
    )


async def cmd_addreview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/addreview @handle — credit a Google review, with a screenshot."""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/addreview @handle</code>\n\n"
            f"Credits one review at {money(REVIEW_RATE_CENTS)}. "
            "I'll ask for the screenshot next.",
            parse_mode=constants.ParseMode.HTML,
        )
        return ConversationHandler.END

    row = find_agent(context.args[0])
    if not row:
        await update.message.reply_text("No one matches that. Check /roster.")
        return ConversationHandler.END

    context.user_data["review_for"] = row["user_id"]
    context.user_data["review_note"] = " ".join(context.args[1:]).strip()
    nm = row["display_name"] or row["name"]
    await update.message.reply_text(
        f"Crediting a review to <b>{esc(nm)}</b>.\n\n"
        "Send the screenshot now, or <code>skip</code> to record it without one.",
        parse_mode=constants.ParseMode.HTML,
    )
    return REVIEW_PHOTO


async def got_review_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    agent_id = context.user_data.pop("review_for", None)
    note = context.user_data.pop("review_note", "")
    if not agent_id:
        await update.message.reply_text("That expired — send /addreview again.")
        return ConversationHandler.END

    photo_id = None
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
    elif (update.message.text or "").strip().lower() not in ("skip", "-"):
        await update.message.reply_text(
            "Send a screenshot, or <code>skip</code> to record it without one.",
            parse_mode=constants.ParseMode.HTML,
        )
        context.user_data["review_for"] = agent_id
        context.user_data["review_note"] = note
        return REVIEW_PHOTO

    today = now().date()
    async with write_lock:
        run(
            "INSERT INTO reviews (agent_id, the_date, photo_id, note, added_by, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (agent_id, today.isoformat(), photo_id, note,
             update.effective_user.id, now().isoformat()),
        )

    first, last = month_bounds(today)
    total = len(reviews_for(agent_id, first, last))
    nm = display_name_of(agent_id, str(agent_id))
    await update.message.reply_text(
        f"✅ Review credited to <b>{esc(nm)}</b>"
        + ("" if photo_id else " (no screenshot)") + ".\n"
        f"{total} this month — {money(total * REVIEW_RATE_CENTS)}.",
        parse_mode=constants.ParseMode.HTML,
    )
    try:
        await context.bot.send_message(
            agent_id,
            f"⭐ A Google review has been credited to you "
            f"({money(REVIEW_RATE_CENTS)}).\n\n"
            f"That's {total} this month. Check /mytime.",
        )
    except Exception:
        pass
    return ConversationHandler.END


async def cmd_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reviews credited this month."""
    if not is_admin(update.effective_user.id):
        return
    first = parse_month(context.args)
    _, last = month_bounds(first)
    rows = q(
        "SELECT agent_id, COUNT(*) c FROM reviews WHERE the_date BETWEEN ? AND ? "
        "GROUP BY agent_id",
        (first.isoformat(), last.isoformat()),
    )
    lines = [f"⭐ <b>Reviews — {first.strftime('%B %Y')}</b>", ""]
    if not rows:
        lines.append("None credited yet.")
    total = 0
    for r in sorted(rows, key=lambda x: -x["c"]):
        nm = display_name_of(r["agent_id"], str(r["agent_id"]))
        lines.append(
            f"{esc(nm)} — {r['c']} × {money(REVIEW_RATE_CENTS)} "
            f"= {money(r['c'] * REVIEW_RATE_CENTS)}"
        )
        total += r["c"]
    if total:
        lines += ["", f"<b>{total} review(s) · {money(total * REVIEW_RATE_CENTS)}</b>"]
    lines.append("\n<code>/addreview @handle</code> to credit one.")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=constants.ParseMode.HTML
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
        pay = f"  {money(sh['cents'])}" if sh["cents"] else ""
        ot = f"  (+{hhmm(sh['ot'])} OT)" if sh.get("ot") else ""
        lines.append(
            f"{sh['date'].strftime('%a %-d %b')}  {a}–{b}  "
            f"<b>{hhmm(sh['minutes'])}</b>{ot}{pay}{flag}  <code>#{r['id']}</code>"
        )
    if t["shifts"]:
        lines += [
            "",
            f"<b>{len(t['shifts'])} shift(s) · {hhmm(t['minutes'])}</b>",
        ]
        if t["cents"]:
            lines.append(
                f"<b>{money(t['cents'])}</b> at "
                f"{money(rate_for(user.id, now().date()))}/hour"
            )
            if t.get("overtime"):
                lines.append(f"<i>includes {hhmm(t['overtime'])} overtime</i>")
    revs = reviews_for(user.id, first, last)
    if revs:
        rc = len(revs) * REVIEW_RATE_CENTS
        lines.append(
            f"⭐ {len(revs)} review(s) × {money(REVIEW_RATE_CENTS)} = <b>{money(rc)}</b>"
        )
        lines.append(f"<b>Total: {money(t['cents'] + rc)}</b>")
    if t["open"]:
        lines.append("\n⏱ You have a shift still clocked in.")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=constants.ParseMode.HTML
    )


async def cmd_setrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setrate 14.50  (team)  ·  /setrate 16 @handle  (one person)"""
    if not is_owner(update.effective_user.id):
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


def week_report(first: date, last: date) -> str:
    """Hours, pay and anything that needs a look, for one week."""
    lines = [
        f"📊 <b>Week of {first.strftime('%-d %b')} – {last.strftime('%-d %b %Y')}</b>",
        "",
    ]

    # ---- hours and pay per agent
    people = []
    total_min = total_cents = 0
    for a in q("SELECT * FROM agents WHERE status='active' ORDER BY name"):
        t = timesheet(a["user_id"], first, last)
        revs = len(reviews_for(a["user_id"], first, last))
        if not t["shifts"] and not revs and not t["open"]:
            continue
        rc = revs * REVIEW_RATE_CENTS
        people.append((a, t, revs, rc))
        total_min += t["minutes"]
        total_cents += t["cents"] + rc

    if people:
        lines.append("<b>Worked</b>")
        for a, t, revs, rc in sorted(people, key=lambda x: -x[1]["minutes"]):
            nm = a["display_name"] or a["name"]
            bit = f"{esc(nm)} — {len(t['shifts'])} shift(s), {hhmm(t['minutes'])}"
            if t.get("overtime"):
                bit += f" (+{hhmm(t['overtime'])} OT)"
            if t["cents"] or rc:
                bit += f" · {money(t['cents'] + rc)}"
            if revs:
                bit += f" ({revs}⭐)"
            lines.append(bit)
        lines += ["", f"<b>Team: {hhmm(total_min)} · {money(total_cents)}</b>"]
    else:
        lines.append("<i>Nothing logged this week yet.</i>")

    # ---- things worth a look
    flags = []

    auto = q(
        """SELECT te.*, a.display_name, a.name FROM time_entries te
           JOIN agents a ON a.user_id = te.agent_id
           WHERE te.status='auto' AND te.the_date BETWEEN ? AND ?""",
        (first.isoformat(), last.isoformat()),
    )
    for r in auto:
        who = r["display_name"] or r["name"]
        flags.append(
            f"⏰ {esc(who)} missed a clock-out on "
            f"{date.fromisoformat(r['the_date']).strftime('%a')} "
            f"(#{r['id']})"
        )

    unros = q(
        """SELECT te.*, a.display_name, a.name FROM time_entries te
           JOIN agents a ON a.user_id = te.agent_id
           WHERE te.slot_id IS NULL AND te.the_date BETWEEN ? AND ?""",
        (first.isoformat(), last.isoformat()),
    )
    for r in unros:
        who = r["display_name"] or r["name"]
        flags.append(
            f"❓ {esc(who)} clocked in unrostered on "
            f"{date.fromisoformat(r['the_date']).strftime('%a')} (#{r['id']})"
        )

    # rostered, the slot has passed, but never clocked in
    today = now().date()
    noshow = q(
        """SELECT su.user_id, s.label, d.name AS day_name, d.the_date,
                  a.display_name, a.name
           FROM signups su
           JOIN slots s ON s.id = su.slot_id
           JOIN days d ON d.id = s.day_id
           JOIN agents a ON a.user_id = su.user_id
           WHERE d.the_date BETWEEN ? AND ? AND d.the_date < ?
             AND NOT EXISTS (
               SELECT 1 FROM time_entries te
               WHERE te.agent_id = su.user_id AND te.the_date = d.the_date
             )
           ORDER BY d.the_date""",
        (first.isoformat(), last.isoformat(), today.isoformat()),
    )
    for r in noshow:
        who = r["display_name"] or r["name"]
        flags.append(
            f"🚫 {esc(who)} never clocked in for "
            f"{r['day_name']} {r['label']}"
        )

    if flags:
        lines += ["", "<b>Worth a look</b>"] + flags[:15]
        if len(flags) > 15:
            lines.append(f"<i>+{len(flags) - 15} more</i>")
    else:
        lines += ["", "✅ <i>Nothing flagged.</i>"]

    # ---- coverage of the live week
    w = q1(
        "SELECT * FROM weeks WHERE start_date=? ORDER BY id DESC LIMIT 1",
        (first.isoformat(),),
    )
    if w:
        st = week_stats(w["id"])
        if st["gaps"]:
            lines += ["", f"⚠️ <b>{len(st['gaps'])} slot(s) uncovered</b>"]
            for g in st["gaps"][:6]:
                lines.append(f"  {g['name']} {g['label']}")
            if len(st["gaps"]) > 6:
                lines.append(f"  +{len(st['gaps']) - 6} more")

    return "\n".join(lines)


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """This week at a glance — hours, pay and anything odd."""
    if not is_admin(update.effective_user.id):
        return
    target = now().date()
    if context.args:
        try:
            target = date.fromisoformat(context.args[0])
        except ValueError:
            await update.message.reply_text(
                "Usage: <code>/week</code> or <code>/week 2026-09-07</code>",
                parse_mode=constants.ParseMode.HTML,
            )
            return
    first, last = week_bounds(target)
    await update.message.reply_text(
        week_report(first, last), parse_mode=constants.ParseMode.HTML
    )


async def job_week_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Monday morning summary of the week just gone."""
    if now().weekday() != 0:
        return
    last_mon = now().date() - timedelta(days=7)
    first, last = week_bounds(last_mon)
    text = week_report(first, last)
    for aid in admin_ids():
        try:
            await context.bot.send_message(
                aid, text, parse_mode=constants.ParseMode.HTML
            )
        except Exception:
            pass
    log.info("Weekly digest sent for %s", first)


async def cmd_timesheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Everyone's hours for a month, or one agent's shifts with entry numbers."""
    if not is_admin(update.effective_user.id):
        return

    # /timesheet @handle — that person's shifts, with the numbers /fixtime needs
    who = None
    rest = []
    for a in context.args:
        if who is None and not re.fullmatch(r"\d{4}-\d{2}", a):
            cand = find_agent(a)
            if cand:
                who = cand
                continue
        rest.append(a)

    if who:
        first = parse_month(rest)
        _, last = month_bounds(first)
        t = timesheet(who["user_id"], first, last)
        nm = who["display_name"] or who["name"]
        lines = [
            f"🕐 <b>{esc(nm)} — {first.strftime('%B %Y')}</b>", ""
        ]
        if not t["shifts"] and not t["open"]:
            lines.append("Nothing logged.")
        for sh in t["shifts"]:
            r = sh["row"]
            a_t = datetime.fromisoformat(r["clock_in"]).strftime("%H:%M")
            b_t = datetime.fromisoformat(r["clock_out"]).strftime("%H:%M")
            flag = " ⚠️" if r["status"] == "auto" else ""
            if r["status"] == "edited":
                flag = " ✏️"
            if sh.get("duplicate"):
                flag += " ♻️ already paid"
            slot = q1(
                "SELECT label FROM slots WHERE id=?", (r["slot_id"],)
            ) if r["slot_id"] else None
            where = slot["label"] if slot else "unrostered"
            lines.append(
                f"<code>#{r['id']}</code>  "
                f"{sh['date'].strftime('%a %-d %b')}  {a_t}–{b_t}  "
                f"{hhmm(sh['minutes'])}  {money(sh['cents'])}{flag}"
                f"\n      <i>{esc(where)}</i>"
            )
        if t["shifts"]:
            lines += [
                "",
                f"<b>{len(t['shifts'])} shift(s) · {hhmm(t['minutes'])} · "
                f"{money(t['cents'])}</b>",
            ]
        if t["open"]:
            lines.append("⏱ Still clocked in.")
        lines += [
            "",
            "<code>/fixtime 42 11:00 17:30</code> to correct one",
            "<code>/fixtime 42 delete</code> to remove it",
        ]
        await update.message.reply_text(
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
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
    if not is_owner(update.effective_user.id):
        return
    first = parse_month(context.args)
    _, last = month_bounds(first)

    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow([
        "entry", "agent", "telegram_id", "date", "day", "slot",
        "clock_in", "clock_out", "actual_hours", "shift_hours", "ot_hours",
        "paid_hours", "rate", "pay", "status",
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
                f"{sh.get('base', 0) / 60:.2f}",
                f"{sh.get('ot', 0) / 60:.2f}",
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
        await flag_to_admins(
            context.bot,
            f"⚠️ <b>Missed clock-out</b>\n\n"
            f"<b>{esc(display_name_of(r['agent_id'], str(r['agent_id'])))}</b> "
            f"didn't clock out. I closed it at {end.strftime('%H:%M')} "
            f"({hhmm(paid)} credited).\n\n"
            f"<code>/fixtime {r['id']} 10:00 18:00</code> to correct it.",
        )
