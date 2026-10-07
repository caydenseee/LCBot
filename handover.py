"""The closing handover flow in chat.

Part of the LC avails bot. Shared helpers live in core.py.
"""

from core import *  # noqa: F401,F403

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


async def on_handover_carry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Everything still open, nothing new from this shift."""
    query = update.callback_query
    user = query.from_user
    if not await gate_cb(query):
        return
    running = open_cases()
    if not running:
        await query.answer("Nothing outstanding.", show_alert=True)
        return
    today = now().date()
    cases = [case_row_to_dict(c) for c in running]
    who = display_name_of(user.id, user.full_name)
    text = render_handover(cases, who, today)
    text = text.replace(
        "Open Cases", "Open Cases\n↩️ Still open — nothing new from my shift", 1
    )
    async with write_lock:
        run(
            "INSERT INTO handovers (agent_id, the_date, body, created_at) "
            "VALUES (?,?,?,?)",
            (user.id, today.isoformat(),
             "\n\n".join(render_case(c) for c in cases), now().isoformat()),
        )
    posted = await post_ops(context.bot, text)
    await query.answer("Carried forward ✓")
    await query.edit_message_text(
        f"✅ Handover posted — {len(cases)} case(s) still open."
        if posted else text,
        parse_mode=None,
    )


def handover_header(the_date: date) -> str:
    return (
        "⭐️ Live Chat Agent Closing Handover ⭐️\n\n"
        "> I have closed the tickets on my shift: ✅\n\n"
        f"🔸{the_date.strftime('%-d %b %Y')}, Open Cases"
    )


async def on_handover_none(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    if not await gate_cb(query):
        return
    today = now().date()
    cleared = [case_row_to_dict(c) for c in open_cases()]
    async with write_lock:
        for c in open_cases():
            close_case(c["id"], user.id)
        run(
            "INSERT INTO handovers (agent_id, the_date, body, created_at) "
            "VALUES (?,?,?,?)",
            (user.id, today.isoformat(), "", now().isoformat()),
        )
    text = render_handover(
        [], display_name_of(user.id, user.full_name), today, cleared
    )
    posted = await post_ops(context.bot, text)
    await query.answer("Nothing to hand over ✓")
    await query.edit_message_text(
        "✅ Handover posted — nothing outstanding.\n\nThanks, enjoy your evening."
        if posted else text,
        parse_mode=None,
    )


def case_picker(keep: set) -> InlineKeyboardMarkup:
    rows = []
    for c in open_cases():
        mark = "✅" if c["id"] in keep else "☑️"
        who = display_name_of(c["agent_id"], "")
        due = case_due(date.fromisoformat(c["the_date"]), c["prio"])
        label = f"{mark} {c['prio']} {c['username']}"
        if c["channel"]:
            label += f" · {CHANNEL_NAMES.get(c['channel'], '')}"
        if who:
            label += f" · {who.split()[0]}"
        if sla_mark(due):
            label += f" · {sla_mark(due)}"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"hk:{c['id']}")])
    rows.append([InlineKeyboardButton("➕ Add a new case", callback_data="hk:new")])
    rows.append([InlineKeyboardButton("📤 Post handover", callback_data="hk:post")])
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data="hk:cancel")])
    return InlineKeyboardMarkup(rows)


async def on_handover_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Tick which of the running cases are still live."""
    query = update.callback_query
    choice = query.data.split(":", 1)[1]
    if choice == "cancel":
        await query.answer()
        for k in ("ho_cases", "ho_draft", "ho_keep"):
            context.user_data.pop(k, None)
        await query.edit_message_text(
            "Handover cancelled — nothing was posted.\n\n"
            "/handover to start again."
        )
        return ConversationHandler.END
    keep = context.user_data.setdefault(
        "ho_keep", {c["id"] for c in open_cases()}
    )

    if choice == "new":
        await query.answer()
        return await show_section(query, context, 0)

    if choice == "post":
        await query.answer()
        return await finish_handover(update, context)

    keep.symmetric_difference_update({int(choice)})
    await query.answer()
    try:
        await query.edit_message_text(
            PICKER_HELP, parse_mode=constants.ParseMode.HTML,
            reply_markup=case_picker(keep),
        )
    except BadRequest:
        pass
    return HO_PICK


async def finish_handover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Close what wasn't kept, save what's new, and post."""
    query = update.callback_query
    user = query.from_user
    today = now().date()
    keep = context.user_data.get("ho_keep")
    if keep is None:
        keep = {c["id"] for c in open_cases()}
    new_cases = context.user_data.get("ho_cases", [])

    cases, closed = [], []
    async with write_lock:
        for c in open_cases():
            if c["id"] in keep:
                cases.append(case_row_to_dict(c))
            else:
                close_case(c["id"], user.id)
                closed.append(case_row_to_dict(c))
        for d in new_cases:
            insert_case(user.id, d, today)
            cases.append(d)
        run(
            "INSERT INTO handovers (agent_id, the_date, body, created_at) "
            "VALUES (?,?,?,?)",
            (user.id, today.isoformat(),
             "\n\n".join(render_case(c) for c in cases), now().isoformat()),
        )

    who = display_name_of(user.id, user.full_name)
    text = render_handover(cases, who, today, closed)
    posted = await post_ops(context.bot, text)
    for k in ("ho_cases", "ho_draft", "ho_keep"):
        context.user_data.pop(k, None)

    if posted:
        msg = f"✅ Handover posted — {len(cases)} open case(s)"
        msg += f", {len(closed)} closed." if closed else "."
        await query.edit_message_text(msg, parse_mode=None)
    else:
        await query.edit_message_text(text, parse_mode=None)
    return ConversationHandler.END


async def on_handover_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start a handover: review running cases first, then add new ones."""
    query = update.callback_query
    if not await gate_cb(query):
        return ConversationHandler.END
    await query.answer()
    context.user_data["ho_cases"] = []
    running = open_cases()
    if running:
        context.user_data["ho_keep"] = {c["id"] for c in running}
        await query.edit_message_text(
            PICKER_HELP, parse_mode=constants.ParseMode.HTML,
            reply_markup=case_picker(context.user_data["ho_keep"]),
        )
        return HO_PICK

    context.user_data["ho_keep"] = set()
    return await show_section(query, context, 1)


async def show_section(query, context, n: int = 0) -> int:
    """Step 1 — open now, or follow up."""
    running = open_cases()
    rows = [
        [InlineKeyboardButton("🔸 Open now", callback_data="hs:open")],
        [InlineKeyboardButton("🔹 Follow up next working day",
                              callback_data="hs:follow")],
    ]
    if running:
        rows.append([InlineKeyboardButton("◀️ Back to the case list",
                                          callback_data="hs:back")])
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data="hs:cancel")])
    title = f"<b>Case {n}</b>" if n else "<b>New case</b>"
    await query.edit_message_text(
        f"{title}\n\nIs this open now, or for the next working day?",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return HO_SECTION


async def show_prio(query, context) -> int:
    """Step 2 — how urgent."""
    await query.edit_message_text(
        "<b>How urgent?</b>",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{e} {n}", callback_data=f"hp:{e}")
             for e, n in PRIORITIES],
            [InlineKeyboardButton("◀️ Back", callback_data="hp:back"),
             InlineKeyboardButton("✖️ Cancel", callback_data="hp:cancel")],
        ]),
    )
    return HO_PRIO


async def show_platform(query, context) -> int:
    """Step 3 — where it came in."""
    d = context.user_data.get("ho_draft", {})
    await query.edit_message_text(
        f"{d.get('prio', '')}  <b>Where did it come in?</b>",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"▫️{p}", callback_data=f"hf:{p}")]
             for p in PLATFORMS]
            + [[InlineKeyboardButton("◀️ Back", callback_data="hf:back"),
                InlineKeyboardButton("✖️ Cancel", callback_data="hf:cancel")]]
        ),
    )
    return HO_PLATFORM


async def show_store(query, context) -> int:
    """Step 4 — Duoke stores only."""
    rows = [
        [InlineKeyboardButton(f"{flag}{store}", callback_data=f"hb:{i}")]
        for i, (flag, store) in enumerate(STORES)
    ]
    rows.append([InlineKeyboardButton("◀️ Back", callback_data="hb:back"),
                 InlineKeyboardButton("✖️ Cancel", callback_data="hb:cancel")])
    await query.edit_message_text(
        "<b>Which store?</b>",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return HO_STORE


def case_head(d: dict) -> str:
    head = f"{d['prio']} ▫️{d['platform']}"
    if d.get("store"):
        head += f" · {d['flag']}{d['store']}"
    channel = channel_for(d["platform"], d.get("store"))
    if channel:
        head += f"\n{CHANNEL_NAMES[channel]}"
    return head


def field_prompt(d: dict) -> str:
    """The question for the step the draft is on."""
    step = d.get("_step", 0)
    _, label, required = CASE_FIELDS[step]
    examples = {
        "username": "kiemmengkoo",
        "order_no": "2609046GFY4T9B",
        "product": "Sonos Move Gen 2",
        "happened": "Customer says the speaker won't charge",
        "done": "Asked for a photo of the charging light",
        "need": "Arrange a replacement once the photo comes in",
    }
    key = CASE_FIELDS[step][0]
    tail = "/skip if nothing · " if not required else ""
    return (
        f"{case_head(d)}\n\n"
        f"<b>{step + 1}/{len(CASE_FIELDS)} · {label}</b>"
        f"{'' if required else ' (optional)'}\n"
        f"<i>e.g. {esc(examples[key])}</i>\n\n"
        f"<i>{tail}/back to change the last answer · /cancel to stop</i>"
    )


async def ask_for_case(query, context) -> int:
    """After the buttons, ask for each part of the case in turn."""
    d = context.user_data["ho_draft"]
    d["_step"] = 0
    await query.edit_message_text(
        field_prompt(d), parse_mode=constants.ParseMode.HTML
    )
    return HO_BODY


async def cancel_handover(query, context) -> int:
    for k in ("ho_cases", "ho_draft", "ho_keep"):
        context.user_data.pop(k, None)
    await query.edit_message_text(
        "Handover cancelled — nothing was posted.\n\n/handover to start again."
    )
    return ConversationHandler.END


async def on_ho_section(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "cancel":
        return await cancel_handover(query, context)
    if choice == "back":
        keep = context.user_data.setdefault(
            "ho_keep", {c["id"] for c in open_cases()})
        await query.edit_message_text(
            PICKER_HELP, parse_mode=constants.ParseMode.HTML,
            reply_markup=case_picker(keep),
        )
        return HO_PICK
    context.user_data["ho_draft"] = {"section": choice}
    return await show_prio(query, context)


async def on_ho_prio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "cancel":
        return await cancel_handover(query, context)
    if choice == "back":
        n = len(context.user_data.get("ho_cases", [])) + 1
        return await show_section(query, context, n)
    context.user_data["ho_draft"]["prio"] = choice
    return await show_platform(query, context)


async def on_ho_platform(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Duoke needs a store; Livechat doesn't, so skip that step for it."""
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "cancel":
        return await cancel_handover(query, context)
    if choice == "back":
        return await show_prio(query, context)

    d = context.user_data["ho_draft"]
    d["platform"] = choice
    if choice != "DUOKE":
        d["flag"], d["store"] = "", ""
        return await ask_for_case(query, context)
    return await show_store(query, context)


async def on_ho_store(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "cancel":
        return await cancel_handover(query, context)
    if choice == "back":
        return await show_platform(query, context)

    flag, store = STORES[int(choice)]
    d = context.user_data["ho_draft"]
    d["flag"], d["store"] = flag, store
    return await ask_for_case(query, context)


async def on_ho_back_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/back while typing — the previous question, or the buttons before them."""
    d = context.user_data.get("ho_draft", {})
    if d.get("_step", 0) > 0:
        d["_step"] -= 1
        await update.message.reply_text(
            field_prompt(d), parse_mode=constants.ParseMode.HTML
        )
        return HO_BODY
    rows = [[InlineKeyboardButton("◀️ Yes, change it", callback_data="hf:back")],
            [InlineKeyboardButton("✖️ Cancel the handover",
                                  callback_data="hf:cancel")]]
    if d.get("platform") == "DUOKE":
        rows[0] = [InlineKeyboardButton("◀️ Pick a different store",
                                        callback_data="hb:back")]
    await update.message.reply_text(
        "Go back a step?", reply_markup=InlineKeyboardMarkup(rows)
    )
    return HO_BODY


async def on_ho_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """One answer per message, until every required part is in."""
    d = context.user_data["ho_draft"]
    step = d.get("_step", 0)
    key, label, _ = CASE_FIELDS[step]
    text = update.message.text.strip()
    if len(text) < 2:
        await update.message.reply_text(
            f"I need the {label.lower()} here. Try again, or /cancel."
        )
        return HO_BODY
    d[key] = text
    return await next_field(update, context)


async def on_ho_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/skip — only for the optional parts."""
    d = context.user_data["ho_draft"]
    key, label, required = CASE_FIELDS[d.get("_step", 0)]
    if required:
        await update.message.reply_text(
            f"{label} can't be skipped, the Online team needs it. "
            "Type it in, or /cancel."
        )
        return HO_BODY
    d[key] = ""
    return await next_field(update, context)


async def next_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = context.user_data["ho_draft"]
    d["_step"] = d.get("_step", 0) + 1
    if d["_step"] < len(CASE_FIELDS):
        await update.message.reply_text(
            field_prompt(d), parse_mode=constants.ParseMode.HTML
        )
        return HO_BODY

    d.pop("_step", None)
    d["body"] = compose_body(d)
    context.user_data.setdefault("ho_cases", []).append(d)
    context.user_data.pop("ho_draft", None)
    n = len(context.user_data["ho_cases"])
    await update.message.reply_text(
        f"✅ Case saved ({n} new this shift).\n\n"
        f"{case_head(d)}\n{d['username']}\n{d['body']}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add another case", callback_data="hm:more")],
            [InlineKeyboardButton("📤 Post handover", callback_data="hm:post")],
            [InlineKeyboardButton("✖️ Cancel", callback_data="hm:cancel")],
        ]),
    )
    return HO_MORE


async def on_ho_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    choice = query.data.split(":")[1]
    if choice == "cancel":
        await query.answer()
        for k in ("ho_cases", "ho_draft", "ho_keep"):
            context.user_data.pop(k, None)
        await query.edit_message_text(
            "Handover cancelled — nothing was posted.\n\n"
            "/handover to start again."
        )
        return ConversationHandler.END
    if choice == "more":
        await query.answer()
        n = len(context.user_data.get("ho_cases", [])) + 1
        return await show_section(query, context, n)
    await query.answer()
    return await finish_handover(update, context)


async def got_handover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    body = update.message.text.strip()
    if len(body) < 10:
        await update.message.reply_text(
            "That looks too short — send the case details, or /cancel to skip."
        )
        return HANDOVER_TEXT

    today = now().date()
    async with write_lock:
        run(
            "INSERT INTO handovers (agent_id, the_date, body, created_at) "
            "VALUES (?,?,?,?)",
            (user.id, today.isoformat(), body, now().isoformat()),
        )

    text = (
        handover_header(today)
        + "\n\n" + body
        + f"\n\n— {display_name_of(user.id, user.full_name)}"
    )
    posted = await post_ops(context.bot, text)
    if posted:
        await update.message.reply_text(
            "✅ Handover posted to TC Online. Thanks!"
        )
    else:
        await update.message.reply_text(text, parse_mode=None)
        await update.message.reply_text("Tap the message above to copy it.")
    return ConversationHandler.END


async def cmd_handover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post a handover outside of clocking out."""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    if not await gate(update):
        return
    running = open_cases()
    note = ""
    if running:
        note = f"\n\n<i>{len(running)} case(s) still open from earlier shifts.</i>"
    await update.message.reply_text(
        "<b>Closing handover</b>\n\nAnything for the next agent?" + note,
        parse_mode=constants.ParseMode.HTML,
        reply_markup=handover_keyboard(running),
    )


async def cmd_handovers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recent handovers, for admins."""
    if not is_admin(update.effective_user.id):
        return
    rows = q(
        "SELECT * FROM handovers ORDER BY created_at DESC LIMIT 10"
    )
    if not rows:
        await update.message.reply_text("No handovers recorded yet.")
        return
    lines = ["<b>Recent handovers</b>", ""]
    for r in rows:
        nm = display_name_of(r["agent_id"], str(r["agent_id"]))
        when = datetime.fromisoformat(r["created_at"]).strftime("%-d %b %H:%M")
        state = "nothing outstanding" if not r["body"] else "open cases"
        lines.append(f"{when} — <b>{esc(nm)}</b>, {state}")
    await reply_long(update.message, 
        "\n".join(lines), parse_mode=constants.ParseMode.HTML
    )
