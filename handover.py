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


async def sync_store_chats(bot, agent_id: int, closed_ids=(),
                           post_new: bool = True) -> None:
    """After a handover: new cases to their store chats (unless the agent
    chose TC Online only), and closed ones updated either way."""
    if post_new:
        await post_new_cases_to_chats(bot, agent_id)
    else:
        skip_store_chats()
    for cid in closed_ids:
        await refresh_case_post(bot, cid)
    await notify_pms(bot, agent_id)


async def cmd_pmalerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pmalerts on|off — the short DM platform managers get about new cases."""
    if not is_admin(update.effective_user.id):
        return
    want = (context.args[0].lower() if context.args else "")
    if want in ("on", "off"):
        async with write_lock:
            set_setting("pm_alerts", want)
    state = setting("pm_alerts", "on")
    await update.message.reply_text(
        f"New-case messages to the Online team are <b>{state}</b>.\n"
        "Change with <code>/pmalerts on</code> or <code>/pmalerts off</code>.",
        parse_mode=constants.ParseMode.HTML,
    )


async def cmd_linkchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/linkchat Shopee SG      — in a store's group: post its new cases here
       /linkchat                — which chat each channel posts to
       /linkchat off Shopee SG  — stop posting that channel's cases"""
    if not is_admin(update.effective_user.id):
        return
    chat = update.effective_chat
    args = " ".join(context.args).strip()
    names = ", ".join(name for _, _, name in CHANNELS)

    if not args:
        lines = ["<b>Store chats</b>", ""]
        for key, flag, name in CHANNELS:
            where = channel_chat(key)
            lines.append(f"{flag} {esc(name)}: " + (
                f"<b>{esc(where.get('title') or str(where['chat']))}</b>"
                if where else "<i>not linked</i>"))
        lines += ["", "To link one, send this inside that store's group:",
                  "<code>/linkchat Shopee SG</code>",
                  "To unlink: <code>/linkchat off Shopee SG</code>"]
        await update.message.reply_text(
            "\n".join(lines), parse_mode=constants.ParseMode.HTML)
        return

    off = args.lower().startswith("off ")
    channel = parse_channel(args[4:] if off else args)
    if not channel:
        await update.message.reply_text(
            f"I don't know that one. Use one of: {names}.")
        return
    if off:
        async with write_lock:
            set_setting(f"chat:{channel}", "")
        await update.message.reply_text(
            f"Unlinked. New {CHANNEL_NAMES[channel]} cases won't be posted to a chat.")
        return
    if chat.type not in (constants.ChatType.GROUP, constants.ChatType.SUPERGROUP):
        await update.message.reply_text(
            "Send this inside the store's group chat, so I know where to post.")
        return

    thread = update.message.message_thread_id if update.message.is_topic_message else None
    async with write_lock:
        set_setting(f"chat:{channel}", json.dumps(
            {"chat": chat.id, "thread": thread, "title": chat.title or ""}))
    await update.message.reply_text(
        f"✅ Linked. New {CHANNEL_NAMES[channel]} cases will be posted here "
        "whenever a handover is posted, and updated as they're closed."
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
    await sync_store_chats(context.bot, user.id)
    await query.answer("Carried forward ✓")
    await query.edit_message_text(
        f"✅ Handover posted — {len(cases)} case(s) still open."
        if posted else text,
        parse_mode=None,
    )


def handover_header(the_date: date) -> str:
    return (
        "⭐️ Live Chat Agent Closing Handover ⭐️\n\n"
        f"🔸{the_date.strftime('%-d %b %Y')}, Open Cases"
    )


async def on_handover_none(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    if not await gate_cb(query):
        return
    today = now().date()
    cleared = [case_row_to_dict(c) for c in open_cases()]
    closed_ids = [c["id"] for c in open_cases()]
    async with write_lock:
        for cid in closed_ids:
            close_case(cid, user.id)
        run(
            "INSERT INTO handovers (agent_id, the_date, body, created_at) "
            "VALUES (?,?,?,?)",
            (user.id, today.isoformat(), "", now().isoformat()),
        )
    text = render_handover(
        [], display_name_of(user.id, user.full_name), today, cleared
    )
    posted = await post_ops(context.bot, text)
    await sync_store_chats(context.bot, user.id, closed_ids)
    await query.answer("Nothing to hand over ✓")
    await query.edit_message_text(
        "✅ Handover posted — nothing outstanding.\n\nThanks, enjoy your evening."
        if posted else text,
        parse_mode=None,
    )


def case_picker(keep: set) -> InlineKeyboardMarkup:
    rows = []
    for c in sorted(open_cases(), key=lambda r: (not r["handed_back"], r["id"])):
        mark = "✅" if c["id"] in keep else "☑️"
        who = display_name_of(c["agent_id"], "")
        due = case_due(date.fromisoformat(c["the_date"]), c["prio"])
        label = f"{mark} {c['prio']} {c['username']}"
        if c["handed_back"]:
            label = f"↩️ {label}"
        elif c["on_closed"]:
            label += " · ☑️ON"
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


def needs_post_choice(context) -> bool:
    """Only ask where to post when a new case is headed for a store chat."""
    new = context.user_data.get("ho_cases", [])
    return store_chats_pending(
        channel_for(d["platform"], d.get("store")) for d in new)


async def ask_where_to_post(query, prefix: str) -> None:
    await query.edit_message_text(
        "<b>Where should this handover go?</b>\n\n"
        "TC Online always gets the full handover. Store chats get only "
        "their own new cases.",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 TC Online only",
                                  callback_data=f"{prefix}:send:ops")],
            [InlineKeyboardButton("📤 TC Online + store chats",
                                  callback_data=f"{prefix}:send:all")],
            [InlineKeyboardButton("◀️ Back", callback_data=f"{prefix}:back")],
        ]),
    )


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
        if needs_post_choice(context):
            await ask_where_to_post(query, "hk")
            return HO_PICK
        return await finish_handover(update, context)

    if choice.startswith("send:"):
        await query.answer()
        return await finish_handover(update, context,
                                     store_chats=choice == "send:all")

    if choice != "back":
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


async def finish_handover(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          store_chats: bool = True) -> int:
    """Close what wasn't kept, save what's new, and post."""
    query = update.callback_query
    user = query.from_user
    today = now().date()
    keep = context.user_data.get("ho_keep")
    if keep is None:
        keep = {c["id"] for c in open_cases()}
    new_cases = context.user_data.get("ho_cases", [])

    cases, closed, closed_ids = [], [], []
    async with write_lock:
        for c in open_cases():
            if c["id"] in keep:
                cases.append(case_row_to_dict(c))
            else:
                close_case(c["id"], user.id)
                closed.append(case_row_to_dict(c))
                closed_ids.append(c["id"])
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
    await sync_store_chats(context.bot, user.id, closed_ids, post_new=store_chats)
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
    if d["platform"] == "LIVECHAT":          # the store already says where else
        head += f" · {CHANNEL_NAMES['WEBSTORE']}"
    return head


def field_prompt(d: dict) -> str:
    """The question for the step the draft is on."""
    step = d.get("_step", 0)
    key, label, required = CASE_FIELDS[step]
    example = case_example(key, d.get("platform"), d.get("store"))
    tail = "/skip if nothing yet · " if not required else ""
    return (
        f"{case_head(d)}\n\n"
        f"<b>{step + 1}/{len(CASE_FIELDS)} · {label}</b>"
        f"{'' if required else ' (optional)'}\n"
        f"{esc(CASE_HINTS[key])}\n"
        + (f"<i>e.g. {esc(example)}</i>\n" if example else "")
        + "\n"
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
        reply_markup=more_keyboard(),
    )
    return HO_MORE


def more_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add another case", callback_data="hm:more")],
        [InlineKeyboardButton("📤 Post handover", callback_data="hm:post")],
        [InlineKeyboardButton("✖️ Cancel", callback_data="hm:cancel")],
    ])


async def on_ho_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    if choice == "more":
        await query.answer()
        n = len(context.user_data.get("ho_cases", [])) + 1
        return await show_section(query, context, n)
    await query.answer()
    if choice.startswith("send:"):
        return await finish_handover(update, context,
                                     store_chats=choice == "send:all")
    if choice == "back":
        n = len(context.user_data.get("ho_cases", []))
        await query.edit_message_text(
            f"{n} new case(s) ready. Add another, or post?",
            reply_markup=more_keyboard(),
        )
        return HO_MORE
    if needs_post_choice(context):
        await ask_where_to_post(query, "hm")
        return HO_MORE
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


# --------------------------------------------------------------------------
# /cases — the Online team's view of handover cases
# --------------------------------------------------------------------------

CASE_NOTE = 270


def may_see_cases(user_id: int) -> bool:
    return is_online(user_id) or is_admin(user_id)


def case_button_label(r) -> str:
    due = case_due(date.fromisoformat(r["the_date"]), r["prio"])
    label = f"{r['prio']} {r['username']} · {CHANNEL_NAMES.get(r['channel'] or '', '?')}"
    if r["handed_back"]:
        label = "↩️ " + label
    elif r["sh_closed"]:
        label += " · ☑️SH"
    elif r["on_closed"]:
        label += " · ☑️ON"
    if sla_mark(due):
        label += f" {sla_mark(due)}"
    return label[:60]


CASES_PER_PAGE = 15


def cases_list(user_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    mine = set(online_channels(user_id))
    rows = q("SELECT * FROM ho_cases WHERE closed=0 ORDER BY id")
    own = [r for r in rows if r["channel"] in mine]
    rest = [r for r in rows if r["channel"] not in mine]
    if not rows:
        return "📋 <b>Open cases</b>\n\nNothing open right now 🎉", None
    lines = ["📋 <b>Open cases</b>", ""]
    if mine:
        lines.append(f"Your channels: {len(own)} · Others: {len(rest)}")
    else:
        lines.append(f"{len(rows)} open")
    lines += ["", "↩️ handed back to SH · ☑️SH / ☑️ON one side has closed it",
              "⏰ due today · 🚨 overdue", "", "<i>Tap a case to open it.</i>"]
    # Own channels first, a page at a time: keeps it scannable on a phone and
    # well under Telegram's 100-button limit.
    ordered = [(r, True) for r in own] + [(r, False) for r in rest]
    pages = max(1, -(-len(ordered) // CASES_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    here = f"cs:l:{page}"
    buttons, heading = [], None
    for r, is_own in ordered[page * CASES_PER_PAGE:(page + 1) * CASES_PER_PAGE]:
        if mine and is_own != heading:
            heading = is_own
            title = "— Your channels —" if is_own else "— Other channels —"
            buttons.append([InlineKeyboardButton(title, callback_data=here)])
        buttons.append([InlineKeyboardButton(case_button_label(r),
                                             callback_data=f"cs:v:{r['id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"cs:l:{page - 1}"))
        nav.append(InlineKeyboardButton(f"Page {page + 1}/{pages}", callback_data=here))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"cs:l:{page + 1}"))
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data=here)])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def case_view(case_id: int, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    r = q1("SELECT * FROM ho_cases WHERE id=?", (case_id,))
    if not r:
        return "That case is gone.", InlineKeyboardMarkup(
            [[InlineKeyboardButton("◀️ All cases", callback_data="cs:l")]])
    c = case_row_to_dict(r)
    c["handback"] = ""
    lines = [case_status_line(r)]
    if r["sh_closed"] and not r["closed"]:
        lines.append(f"SH closed it · {display_name_of(r['sh_closed_by'], '')}")
    if r["on_closed"] and not r["closed"]:
        lines.append(f"Online closed it · {display_name_of(r['on_closed_by'], '')}")
    lines += ["", render_case(c)]
    hist = case_history(case_id)[-6:]
    if hist:
        lines += ["", "🕓 History"]
        for h in hist:
            when = datetime.fromisoformat(h["created_at"]).strftime("%-d %b %H:%M")
            who = display_name_of(h["user_id"], "") if h["user_id"] else ""
            line = f"{when} · {who or h['side']}: {h['action']}"
            if h["note"]:
                line += f" — {h['note']}"
            lines.append(line)

    online = is_online(user_id)
    buttons = []
    if not r["closed"]:
        if online and not r["on_closed"]:
            buttons.append([InlineKeyboardButton("✅ Close (Online side)",
                                                 callback_data=f"cs:x:{case_id}")])
        buttons.append([InlineKeyboardButton("📝 Add a note",
                                             callback_data=f"cs:n:{case_id}"),
                        InlineKeyboardButton("✏️ Edit",
                                             callback_data=f"cs:e:{case_id}")])
        if online:
            buttons.append([InlineKeyboardButton("↩️ Hand back to SH",
                                                 callback_data=f"cs:h:{case_id}")])
        if is_admin(user_id):
            buttons.append([InlineKeyboardButton("🔒 Force close (both sides)",
                                                 callback_data=f"cs:f:{case_id}")])
    buttons.append([InlineKeyboardButton("◀️ All cases", callback_data="cs:l")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def cmd_cases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    uid = update.effective_user.id
    if agent_status(uid) != "active" and not is_owner(uid):
        await gate(update)
        return
    if not may_see_cases(uid):
        await update.message.reply_text("Use /handover to see and close cases.")
        return
    text, kb = cases_list(uid)
    await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML,
                                    reply_markup=kb)


async def on_cases_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cs:l list · cs:v view · cs:x Online closes · cs:f admin force-closes"""
    query = update.callback_query
    uid = query.from_user.id
    if not may_see_cases(uid):
        await query.answer("That's for the Online team and admins.", show_alert=True)
        return
    parts = query.data.split(":")
    action = parts[1]
    case_id = int(parts[2]) if len(parts) > 2 else 0    # the page, for cs:l

    if action == "x" and is_online(uid):
        async with write_lock:
            close_case(case_id, uid, side="on")
        await refresh_case_post(context.bot, case_id)
        await query.answer("Closed on the Online side ✓")
    elif action == "f" and is_admin(uid):
        async with write_lock:
            force_close_case(case_id, uid)
        await refresh_case_post(context.bot, case_id)
        await query.answer("Force closed ✓")
    else:
        await query.answer()

    if action == "e":
        text, kb = edit_picker(case_id)
        await query.edit_message_text(text, parse_mode=constants.ParseMode.HTML,
                                      reply_markup=kb)
        return
    if action == "ep":
        prio = parts[3]
        if prio in [e for e, _ in PRIORITIES]:
            async with write_lock:
                save_case_edit(case_id, uid, "prio", prio)
            await refresh_case_post(context.bot, case_id)

    if action == "l":
        text, kb = cases_list(uid, page=case_id)
        parse = constants.ParseMode.HTML
    else:
        text, kb = case_view(case_id, uid)
        parse = None
    try:
        await query.edit_message_text(text, parse_mode=parse, reply_markup=kb)
    except BadRequest:
        pass


async def on_case_note_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """cs:n add a note · cs:h hand back to SH (Online only) — then they type."""
    query = update.callback_query
    uid = query.from_user.id
    _, action, raw = query.data.split(":")
    if not may_see_cases(uid) or (action == "h" and not is_online(uid)):
        await query.answer("You can't do that here.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["case_note"] = (action, int(raw))
    r = q1("SELECT username FROM ho_cases WHERE id=?", (int(raw),))
    who = r["username"] if r else "this case"
    mine = reusable_note(int(raw), uid) if action == "h" else None
    if mine:
        await query.edit_message_text(
            f"↩️ <b>Hand {esc(who)} back to SH</b>\n\nUse your note?\n"
            f"<i>{esc(mine['note'])}</i>",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, hand back with this note",
                                      callback_data=f"cs:hy:{raw}")],
                [InlineKeyboardButton("✏️ Write a different message",
                                      callback_data=f"cs:hw:{raw}")],
                [InlineKeyboardButton("◀️ Back to the case", callback_data=f"cs:v:{raw}")],
            ]),
        )
        return CASE_NOTE
    ask = (f"↩️ <b>Hand {esc(who)} back to SH</b>\n\nWhat do you need from them?"
           if action == "h" else
           f"📝 <b>Note on {esc(who)}</b>\n\nWhat's the latest? "
           "(e.g. refund done, waiting on courier)")
    await query.edit_message_text(ask + "\n\n<i>/cancel to stop</i>",
                                  parse_mode=constants.ParseMode.HTML)
    return CASE_NOTE


async def on_handback_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """cs:hy hand back with the note they just left · cs:hw type a new one ·
    cs:v back to the case."""
    query = update.callback_query
    uid = query.from_user.id
    _, action, raw = query.data.split(":")
    case_id = int(raw)
    await query.answer()
    if action == "hw":
        context.user_data["case_note"] = ("h", case_id)
        r = q1("SELECT username FROM ho_cases WHERE id=?", (case_id,))
        await query.edit_message_text(
            f"↩️ <b>Hand {esc(r['username'] if r else 'this case')} back to SH</b>"
            "\n\nWhat do you need from them?\n\n<i>/cancel to stop</i>",
            parse_mode=constants.ParseMode.HTML)
        return CASE_NOTE
    context.user_data.pop("case_note", None)
    if action == "hy" and is_online(uid):
        mine = reusable_note(case_id, uid)
        if mine:
            async with write_lock:
                hand_back_case(case_id, uid, None)
            await refresh_case_post(context.bot, case_id)
            await announce_handback(context.bot, case_id, uid, mine["note"])
    text, kb = case_view(case_id, uid)
    await query.edit_message_text(
        ("↩️ Handed back to SH.\n\n" if action == "hy" else "") + text, reply_markup=kb)
    return ConversationHandler.END


async def on_case_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action, case_id = context.user_data.pop("case_note", (None, 0))
    note = update.message.text.strip()
    uid = update.effective_user.id
    if not case_id:
        return ConversationHandler.END
    if len(note) < 2:
        context.user_data["case_note"] = (action, case_id)
        await update.message.reply_text("Type a few words, or /cancel.")
        return CASE_NOTE
    side = "on" if is_online(uid) else "sh"
    async with write_lock:
        if action == "h":
            hand_back_case(case_id, uid, note)
        else:
            log_case(case_id, uid, side, "note", note)
    await refresh_case_post(context.bot, case_id)
    if action == "h":
        await announce_handback(context.bot, case_id, uid, note)
    text, kb = case_view(case_id, uid)
    await update.message.reply_text(
        ("↩️ Handed back to SH." if action == "h" else "📝 Note saved.") + "\n\n" + text,
        reply_markup=kb,
    )
    return ConversationHandler.END


# --------------------------------------------------------------------------
# ✏️ Editing a case from /cases
# --------------------------------------------------------------------------

CASE_EDIT = 271
EDIT_LABELS = {"prio": "Urgency", **{k: label for k, label, _ in CASE_FIELDS}}


def edit_picker(case_id: int) -> tuple[str, InlineKeyboardMarkup]:
    r = q1("SELECT * FROM ho_cases WHERE id=?", (case_id,))
    if not r:
        return "That case is gone.", InlineKeyboardMarkup(
            [[InlineKeyboardButton("◀️ All cases", callback_data="cs:l")]])
    rows = [[InlineKeyboardButton(f"{e} {n}", callback_data=f"cs:ep:{case_id}:{e}")
             for e, n in PRIORITIES]]
    # Cases written before the six questions only have free text to show.
    keys = [k for k, _, _ in CASE_FIELDS] if r["order_no"] is not None else ["username"]
    rows += [[InlineKeyboardButton(EDIT_LABELS[k], callback_data=f"cs:ef:{case_id}:{k}")]
             for k in keys]
    rows.append([InlineKeyboardButton("◀️ Back to the case", callback_data=f"cs:v:{case_id}")])
    note = ("" if r["order_no"] is not None else
            "\n\n<i>This case was written before the new format, so only the "
            "urgency and username can be changed.</i>")
    return (f"✏️ <b>Edit {esc(r['username'])}</b>\n\nTap the urgency to change it, "
            f"or pick what to rewrite.{note}"), InlineKeyboardMarkup(rows)


def save_case_edit(case_id: int, user_id: int, key: str, value: str) -> None:
    """Change one part of a case, rebuild its written-out body, and log it."""
    r = q1("SELECT * FROM ho_cases WHERE id=?", (case_id,))
    old = r[key] or ""
    if old == value:
        return
    run(f"UPDATE ho_cases SET {key}=? WHERE id=?", (value or None, case_id))
    r = q1("SELECT * FROM ho_cases WHERE id=?", (case_id,))
    if r["order_no"] is not None:
        d = {k: r[k] or "" for k, _, _ in CASE_FIELDS}
        run("UPDATE ho_cases SET body=? WHERE id=?", (compose_body(d), case_id))
    side = "on" if is_online(user_id) else "sh"
    log_case(case_id, user_id, side, "edited",
             f"{EDIT_LABELS[key]}: {old or '(blank)'} → {value or '(blank)'}")


async def on_case_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """cs:ef:<id>:<field> — ask for the new text."""
    query = update.callback_query
    uid = query.from_user.id
    _, _, raw, key = query.data.split(":", 3)
    if not may_see_cases(uid) or key not in EDIT_LABELS or key == "prio":
        await query.answer("You can't do that here.", show_alert=True)
        return ConversationHandler.END
    r = q1("SELECT * FROM ho_cases WHERE id=?", (int(raw),))
    if not r:
        await query.answer("That case is gone.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["case_edit"] = (int(raw), key)
    optional = key == "done"
    await query.edit_message_text(
        f"✏️ <b>{esc(EDIT_LABELS[key])}</b> for {esc(r['username'])}\n\n"
        f"Now: <i>{esc(r[key] or '(blank)')}</i>\n\n"
        f"Send the new {esc(EDIT_LABELS[key].lower())}."
        + (" Send <code>-</code> to clear it." if optional else "")
        + "\n\n<i>/cancel to leave it as it is</i>",
        parse_mode=constants.ParseMode.HTML,
    )
    return CASE_EDIT


async def on_case_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    case_id, key = context.user_data.pop("case_edit", (0, ""))
    if not case_id:
        return ConversationHandler.END
    uid = update.effective_user.id
    value = update.message.text.strip()
    if key == "done" and value == "-":
        value = ""
    elif len(value) < 2:
        context.user_data["case_edit"] = (case_id, key)
        await update.message.reply_text(
            f"I need the {EDIT_LABELS[key].lower()} here. Try again, or /cancel.")
        return CASE_EDIT
    async with write_lock:
        save_case_edit(case_id, uid, key, value)
    await refresh_case_post(context.bot, case_id)
    text, kb = case_view(case_id, uid)
    await update.message.reply_text(f"✏️ {EDIT_LABELS[key]} updated.\n\n" + text,
                                    reply_markup=kb)
    return ConversationHandler.END
