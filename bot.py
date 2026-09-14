"""Entry point: starts the bot, schedules jobs, wires the commands up."""

from core import *  # noqa: F401,F403
from board import *  # noqa: F401,F403
from people import *  # noqa: F401,F403
from shifts import *  # noqa: F401,F403
from timeclock import *  # noqa: F401,F403
from handover import *  # noqa: F401,F403
from webapp import *  # noqa: F401,F403

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("draft", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def cmd_shiftcall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post the on-duty tags now, or preview them without posting."""
    if not is_admin(update.effective_user.id):
        return

    args = [a.lower() for a in context.args]
    preview = "preview" in args or "test" in args
    target = None
    for a in context.args:
        if a.lower() in ("preview", "test"):
            continue
        try:
            target = date.fromisoformat(a)
        except ValueError:
            await update.message.reply_text(
                "Usage:\n"
                "<code>/shiftcall preview</code> — show it here, post nothing\n"
                "<code>/shiftcall</code> — post it to the group\n"
                "<code>/shiftcall 2026-09-08</code> — a specific day",
                parse_mode=constants.ParseMode.HTML,
            )
            return

    text, reason = build_shift_call(target)
    if not text:
        await update.message.reply_text(f"Nothing to post — {reason}")
        return

    if preview:
        # Mentions are shown as plain text so nobody is notified.
        shown = re.sub(r'<a href="tg://user\?id=\d+">([^<]*)</a>', r"\1", text)
        shown = shown.replace("@", "＠")
        await update.message.reply_text(
            "<b>Preview — nothing has been posted</b>\n"
            "<i>Handles shown as plain text so nobody is pinged.</i>\n\n"
            + shown + "\n\n<i>Send /shiftcall to post it for real.</i>",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    await send_group(
        context.bot, text, thread=SHIFTCALL_THREAD_ID,
        chat_id=SHIFTCALL_CHAT_ID,
        parse_mode=constants.ParseMode.HTML,
    )
    await update.message.reply_text("Posted to the group ✅")


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
    if not is_owner(update.effective_user.id):
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


async def cmd_tidy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete old closed weeks. The live week and all hours are untouched."""
    if not is_owner(update.effective_user.id):
        return

    live = open_week()
    old = q(
        "SELECT * FROM weeks WHERE status='closed'"
        + (" AND id <> ?" if live else "")
        + " ORDER BY id",
        (live["id"],) if live else (),
    )
    if not old:
        await update.message.reply_text("No old weeks to clear.")
        return

    n_sign = q1(
        """SELECT COUNT(*) c FROM signups su JOIN slots s ON s.id=su.slot_id
           JOIN days d ON d.id=s.day_id WHERE d.week_id IN ({})""".format(
            ",".join(str(w["id"]) for w in old)),
    )["c"]

    if not context.args or context.args[0] != "CONFIRM":
        lines = [
            f"This clears <b>{len(old)} closed week(s)</b> and their "
            f"{n_sign} claim(s):",
            "",
        ]
        lines += [f"  · {esc(w['label'])} ({w['start_date']})" for w in old[:10]]
        if len(old) > 10:
            lines.append(f"  · and {len(old) - 10} more")
        lines += [
            "",
            "<b>Kept:</b> the live week, every clock-in record, agents, "
            "rates and fixed rosters.",
            "",
            "To go ahead: <code>/tidy CONFIRM</code>",
        ]
        if live:
            lines.append(f"\n<i>Live week {esc(live['label'])} is safe.</i>")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    ids = ",".join(str(w["id"]) for w in old)
    async with write_lock:
        run(f"""DELETE FROM signups WHERE slot_id IN
                (SELECT s.id FROM slots s JOIN days d ON d.id=s.day_id
                 WHERE d.week_id IN ({ids}))""")
        run(f"DELETE FROM confirmations WHERE week_id IN ({ids})")
        run(f"""DELETE FROM slots WHERE day_id IN
                (SELECT id FROM days WHERE week_id IN ({ids}))""")
        run(f"DELETE FROM days WHERE week_id IN ({ids})")
        run(f"DELETE FROM weeks WHERE id IN ({ids})")

    await update.message.reply_text(
        f"✅ Cleared {len(old)} old week(s).\n"
        "Clock-in records, agents, rates and fixed rosters all kept.",
        parse_mode=constants.ParseMode.HTML,
    )
    log.info("Tidied %d old weeks", len(old))


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
    if not is_owner(update.effective_user.id):
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
    lines = ["<b>What I can do</b>", ""]
    lines += [f"/{c} — {d}" for c, d in AGENT_COMMANDS if c != "help"]

    if role in ("admin", "owner"):
        for title, group in ADMIN_GROUPS:
            lines += ["", f"<b>{title}</b>"]
            lines += [f"/{c} — {d}" for c, d in group]
    if role == "owner":
        lines += ["", "<b>Owner</b>"]
        lines += [f"/{c} — {d}" for c, d in OWNER_EXTRA]

    text = "\n".join(lines)
    # Telegram caps a message at 4096 characters.
    if len(text) > 3900:
        cut = text.rfind("\n\n<b>", 0, 3900)
        await update.message.reply_text(text[:cut], parse_mode=constants.ParseMode.HTML)
        await update.message.reply_text(text[cut:], parse_mode=constants.ParseMode.HTML)
        return
    await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)


async def job_shift_call(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Evening-before group post tagging tomorrow's agents."""
    text, reason = build_shift_call()
    if not text:
        log.info("Shift call skipped: %s", reason)
        return
    try:
        await send_group(
            context.bot, text, thread=SHIFTCALL_THREAD_ID,
            chat_id=SHIFTCALL_CHAT_ID,
            parse_mode=constants.ParseMode.HTML,
        )
    except Exception as e:
        log.warning("Shift call failed: %s", e)


def build_shift_call(for_date: date | None = None) -> tuple[str | None, str]:
    """Returns (message, reason). Message is None when there's nothing to send."""
    target = for_date or (now() + timedelta(days=1)).date()
    if not SHIFTCALL_CHAT_ID:
        return None, "No chat is set for the shift call."
    day = day_row_for(target.isoformat())
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
    taggable = []
    for uid, nm in working.items():
        row = q1("SELECT tag_calls FROM agents WHERE user_id=?", (uid,))
        if row is None or row["tag_calls"]:
            taggable.append(mention(uid, nm))
    if taggable:
        lines.append("\n" + " ".join(taggable))
    return "\n".join(lines), "ok"


async def post_init(app: Application) -> None:
    """Re-arm jobs after a restart."""
    start_web_server(app)
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
    log.info(
        "Group shift call scheduled for %s daily, to chat %s topic %s",
        SHIFT_CALL_TIME, SHIFTCALL_CHAT_ID, SHIFTCALL_THREAD_ID or "(none)",
    )
    app.job_queue.run_repeating(
        job_auto_close, interval=timedelta(minutes=30),
        first=timedelta(minutes=5), name="auto-close",
    )
    bmins = parse_time_token(BACKUP_TIME)
    app.job_queue.run_daily(
        job_backup, time(bmins // 60, bmins % 60, tzinfo=TZ), name="backup",
    )
    dmins = parse_time_token(os.environ.get("DIGEST_TIME", "").strip() or "09:00")
    app.job_queue.run_daily(
        job_week_digest, time(dmins // 60, dmins % 60, tzinfo=TZ),
        name="week-digest",
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
                ASK_SLOTS: [
                    CallbackQueryHandler(on_week_shape, pattern=r"^ws:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_slots),
                ],
                ASK_SHAPE_LABEL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_shape_label)
                ],
                ASK_DAYS: [CallbackQueryHandler(on_week_days, pattern=r"^wd:")],
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
    app.add_handler(CommandHandler("access", cmd_access))
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
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("dropshift", cmd_dropshift)],
            states={
                DROP_PICK: [CallbackQueryHandler(on_drop_pick, pattern=r"^ds:")],
                DROP_REASON: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_drop_reason)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("pickup", cmd_pickup)],
            states={
                PICKUP_PICK: [CallbackQueryHandler(on_pickup_pick, pattern=r"^pu:")],
                PICKUP_REASON: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_pickup_reason)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("swap", cmd_swap)],
            states={
                SWAP_PICK: [CallbackQueryHandler(on_swap_pick, pattern=r"^sw:")],
                SWAP_WHO: [CallbackQueryHandler(on_swap_who, pattern=r"^sq:")],
                SWAP_REASON: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_swap_reason)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    app.add_handler(CommandHandler("dropreqs", cmd_dropreqs))
    app.add_handler(CallbackQueryHandler(on_drop_decision, pattern=r"^dq:[ad]:\d+$"))
    app.add_handler(CommandHandler("clockout", cmd_clockout))
    app.add_handler(CommandHandler("mytime", cmd_mytime))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("payslip", cmd_payslip))
    app.add_handler(CommandHandler("setrate", cmd_setrate))
    app.add_handler(CommandHandler("timesheet", cmd_timesheet))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("addtime", cmd_addtime))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("payroll", cmd_payroll))
    app.add_handler(CommandHandler("openshifts", cmd_openshifts))
    app.add_handler(CommandHandler("clockoutfor", cmd_clockoutfor))
    app.add_handler(CommandHandler("fixtime", cmd_fixtime))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("tidy", cmd_tidy))
    app.add_handler(CommandHandler("removeagent", cmd_removeagent))
    app.add_handler(CommandHandler("avails", cmd_avails))
    app.add_handler(CommandHandler("salaried", cmd_salaried))
    app.add_handler(CommandHandler("tag", cmd_tag))
    app.add_handler(CommandHandler("fixed", cmd_fixed))
    app.add_handler(CommandHandler("capacity", cmd_capacity))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("addslot", cmd_addslot))
    app.add_handler(CommandHandler("delslot", cmd_delslot))
    app.add_handler(CommandHandler("dropslot", cmd_dropslot))
    app.add_handler(CommandHandler("whohas", cmd_whohas))
    app.add_handler(CommandHandler("applyfixed", cmd_applyfixed))
    app.add_handler(CommandHandler("makeadmin", cmd_makeadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("presets", cmd_presets))
    app.add_handler(CommandHandler("savepreset", cmd_savepreset))
    app.add_handler(CallbackQueryHandler(on_toggle, pattern=r"^t:\d+$"))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^[cu]:\d+$"))
    app.add_handler(CallbackQueryHandler(on_plan_toggle, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(on_plan_nav, pattern=r"^pd:"))
    app.add_handler(CallbackQueryHandler(on_clockin_slot, pattern=r"^ci:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_access_decision, pattern=r"^(ap|dn):\d+$"))
    app.add_handler(CallbackQueryHandler(on_plan_quick, pattern=r"^pq:"))
    app.add_handler(CallbackQueryHandler(on_handover_none, pattern=r"^ho:none$"))
    app.add_handler(CallbackQueryHandler(on_handover_carry, pattern=r"^ho:carry$"))
    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(on_handover_add, pattern=r"^ho:add$")
            ],
            states={
                HO_SECTION: [CallbackQueryHandler(on_ho_section, pattern=r"^hs:")],
                HO_PRIO: [CallbackQueryHandler(on_ho_prio, pattern=r"^hp:")],
                HO_PLATFORM: [CallbackQueryHandler(on_ho_platform, pattern=r"^hf:")],
                HO_STORE: [CallbackQueryHandler(on_ho_store, pattern=r"^hb:")],
                HO_BODY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, on_ho_body)
                ],
                HO_MORE: [CallbackQueryHandler(on_ho_more, pattern=r"^hm:")],
                HO_PICK: [CallbackQueryHandler(on_handover_pick, pattern=r"^hk:")],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    app.add_handler(CommandHandler("handover", cmd_handover))
    app.add_handler(CommandHandler("handovers", cmd_handovers))
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("addreview", cmd_addreview)],
            states={
                REVIEW_PHOTO: [
                    MessageHandler(
                        (filters.PHOTO | (filters.TEXT & ~filters.COMMAND)),
                        got_review_photo,
                    )
                ]
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    app.add_handler(CommandHandler("reviews", cmd_reviews))

    log.info("Avails bot running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
