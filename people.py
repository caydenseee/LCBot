"""Access requests, roles and the roster.

Part of the LC avails bot. Shared helpers live in core.py.
"""

from core import *  # noqa: F401,F403

async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Setup helper — reports the IDs you need for the env file."""
    if not is_owner(update.effective_user.id):
        return
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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return ConversationHandler.END

    status = agent_status(user.id)

    if status == "active":
        touch_agent(user, dm_ok=1)
        await update.message.reply_text(
            reply_markup=agent_keyboard(update.effective_user.id),
            text="👋 <b>You're in!</b>\n\n"
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
    sent = await notify_admins(context.bot, text, kb)
    run("UPDATE agents SET req_msgs=? WHERE user_id=?",
        (json.dumps(sent), user.id))
    if not sent:
        log.warning("Access request from %s but no admin could be reached.", user.id)
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
    w = open_week() or latest_week()
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
    settled = (
        f"🔔 <b>Access request</b>\n\n<b>{esc(shown)}</b>\n\n"
        f"{verdict} by {esc(decider.full_name)}"
    )
    try:
        sent = json.loads(row["req_msgs"] or "[]")
    except (json.JSONDecodeError, TypeError):
        sent = []
    if sent:
        await settle_admin_messages(context.bot, sent, settled)
    else:
        try:
            await query.edit_message_text(
                settled, parse_mode=constants.ParseMode.HTML
            )
        except BadRequest:
            pass

    try:
        if approve:
            await context.bot.send_message(
                target_id,
                reply_markup=agent_keyboard(target_id),
                text="👋 <b>You're in!</b>\n\n"
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


async def cmd_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Who approved or declined whom, and when."""
    if not is_admin(update.effective_user.id):
        return

    if context.args:
        row = find_agent(context.args[0])
        if not row:
            await update.message.reply_text("No one matches that. Check /roster.")
            return
        nm = row["display_name"] or row["name"]
        lines = [f"<b>{esc(nm)}</b>", ""]
        lines.append(f"Status: <b>{row['status']}</b>")
        if row["requested_at"]:
            lines.append(
                "Requested: "
                + datetime.fromisoformat(row["requested_at"]).strftime("%-d %b, %H:%M")
            )
        if row["decided_by"]:
            who = display_name_of(row["decided_by"], str(row["decided_by"]))
            when = (
                datetime.fromisoformat(row["decided_at"]).strftime("%-d %b, %H:%M")
                if row["decided_at"] else "unknown time"
            )
            verb = "Approved" if row["status"] == "active" else "Declined"
            lines.append(f"{verb} by <b>{esc(who)}</b> on {when}")
        else:
            lines.append("<i>No decision recorded — joined before approvals existed.</i>")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    rows = q(
        """SELECT * FROM agents WHERE decided_at IS NOT NULL
           ORDER BY decided_at DESC LIMIT 25"""
    )
    pending = q1("SELECT COUNT(*) c FROM agents WHERE status='pending'")["c"]
    lines = ["<b>Access decisions</b>", ""]
    if not rows:
        lines.append("Nothing recorded yet.")
    for r in rows:
        mark = "✅" if r["status"] == "active" else "🚫"
        who = (
            display_name_of(r["decided_by"], str(r["decided_by"]))
            if r["decided_by"] else "?"
        )
        when = (
            datetime.fromisoformat(r["decided_at"]).strftime("%-d %b %H:%M")
            if r["decided_at"] else ""
        )
        lines.append(
            f"{mark} {esc(r['display_name'] or r['name'])} — "
            f"by {esc(who)}, {when}"
        )
    declined = q("SELECT * FROM agents WHERE status='declined'")
    if declined:
        lines += [
            "",
            f"<i>{len(declined)} declined. To let one in, they send /start again "
            "after you clear them with /removeagent.</i>",
        ]
    if pending:
        lines += ["", f"⏳ {pending} waiting now — see /pending"]
    lines.append("\n<code>/access @handle</code> for one person's history.")
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


async def cmd_roster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    if not is_admin(update.effective_user.id):
        return
    rows = q("SELECT * FROM agents WHERE status='active' ORDER BY name")
    waiting = q1("SELECT COUNT(*) c FROM agents WHERE status='pending'")["c"]
    if not rows:
        await update.message.reply_text(
            "Roster is empty. Agents join it by tapping a slot, or by sending me /start."
        )
        return
    def line(r):
        role = role_of(r["user_id"])
        badge = {"owner": "👑", "admin": "🛠"}.get(role, "")
        bits = f"{'🔔' if r['dm_ok'] else '🔕'}{badge} "
        bits += esc(r["display_name"] or r["name"])
        if r["username"]:
            bits += f" @{r['username']}"
        if not r["on_avails"]:
            bits += " · not on avails"
        if r["salaried"]:
            bits += " · salaried"
        bits += f" · <code>{r['user_id']}</code>"
        return bits

    owners = [r for r in rows if role_of(r["user_id"]) == "owner"]
    admins = [r for r in rows if role_of(r["user_id"]) == "admin"]
    agents = [r for r in rows if role_of(r["user_id"]) == "agent"]

    lines = [f"<b>Roster ({len(rows)})</b>"]
    if owners:
        lines += ["", "<b>👑 Owners</b>"] + [line(r) for r in owners]
    if admins:
        lines += ["", "<b>🛠 Admins</b>"] + [line(r) for r in admins]
    if agents:
        lines += ["", f"<b>Agents ({len(agents)})</b>"] + [line(r) for r in agents]

    # Owners set in config may have never messaged the bot.
    missing = [
        uid for uid in ADMIN_IDS
        if not any(r["user_id"] == uid for r in rows)
    ]
    if missing:
        lines += ["", "<b>👑 Owners (config only)</b>"] + [
            f"<code>{uid}</code> — hasn't messaged the bot" for uid in missing
        ]

    lines.append("\n🔕 = hasn't sent me /start, so can't get shift reminders.")
    lines.append(
        "<code>/makeadmin @handle</code> · <code>/removeadmin @handle</code> "
        "· <code>/removeagent @handle</code>"
    )
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


async def cmd_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Who gets @-mentioned in the nightly on-duty post.

    They still appear on the rota either way — this only stops the ping.
    """
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        on = q("SELECT * FROM agents WHERE status='active' AND tag_calls=1 ORDER BY name")
        off = q("SELECT * FROM agents WHERE status='active' AND tag_calls=0 ORDER BY name")
        lines = [f"<b>Tagged in the nightly post ({len(on)})</b>"]
        lines += [f"  {esc(a['display_name'] or a['name'])}" for a in on] or ["  nobody"]
        lines += ["", f"<b>Not tagged ({len(off)})</b>"]
        lines += [f"  {esc(a['display_name'] or a['name'])}" for a in off] or ["  nobody"]
        lines += [
            "",
            "<code>/tag off @handle</code> — stop pinging them",
            "<code>/tag on @handle</code> — start again",
            "",
            "<i>They still show on the rota either way.</i>",
        ]
        await update.message.reply_text(
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    mode = context.args[0].lower()
    if mode not in ("on", "off") or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/tag off @handle</code> or <code>/tag on @handle</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    row = find_agent(context.args[1])
    if not row:
        await update.message.reply_text("No one matches that. Check /roster.")
        return

    want = 1 if mode == "on" else 0
    async with write_lock:
        run("UPDATE agents SET tag_calls=? WHERE user_id=?", (want, row["user_id"]))
    nm = row["display_name"] or row["name"]
    if want:
        msg = f"<b>{esc(nm)}</b> will be tagged in the nightly on-duty post again."
    else:
        msg = (f"<b>{esc(nm)}</b> won't be tagged in the nightly post.\n"
               "They'll still appear on the rota.")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)


async def cmd_salaried(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark someone as salaried, so their hours aren't paid hourly."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        sal = q("SELECT * FROM agents WHERE status='active' AND salaried=1 ORDER BY name")
        hourly = q("SELECT * FROM agents WHERE status='active' AND salaried=0 ORDER BY name")
        lines = [f"<b>Salaried — no hourly pay ({len(sal)})</b>"]
        lines += [f"  {esc(a['display_name'] or a['name'])}" for a in sal] or ["  nobody"]
        lines += ["", f"<b>Paid hourly ({len(hourly)})</b>"]
        lines += [f"  {esc(a['display_name'] or a['name'])}" for a in hourly] or ["  nobody"]
        lines += [
            "",
            "<code>/salaried on @handle</code> — stop hourly pay",
            "<code>/salaried off @handle</code> — pay them hourly again",
            "",
            "<i>Salaried agents still appear on the board, still clock in "
            "and out, and their hours are still recorded.</i>",
        ]
        await update.message.reply_text(
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    mode = context.args[0].lower()
    if mode not in ("on", "off") or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/salaried on @handle</code> or "
            "<code>/salaried off @handle</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    row = find_agent(context.args[1])
    if not row:
        await update.message.reply_text("No one matches that. Check /roster.")
        return

    want = 1 if mode == "on" else 0
    async with write_lock:
        run("UPDATE agents SET salaried=? WHERE user_id=?", (want, row["user_id"]))
    nm = row["display_name"] or row["name"]
    if want:
        msg = (f"<b>{esc(nm)}</b> is salaried — livechat hours won't be paid "
               "hourly.\nThey still show on the board and still clock in and out.")
    else:
        msg = (f"<b>{esc(nm)}</b> is back on hourly pay at "
               f"{money(rate_for(row['user_id'], now().date()))}/hour.")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)


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

    w = open_week() or latest_week()
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
        w = open_week() or latest_week()
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
