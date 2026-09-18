"""The Mini App: home, week, hours, handover and team.

Part of the LC avails bot. Shared helpers live in core.py.
"""

from core import *  # noqa: F401,F403

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
            "📱 Open the app", web_app=WebAppInfo(url=app_url())
        )]]
    )
    await update.message.reply_text(
        "Your hours and pay for this month:", reply_markup=kb
    )


def miniapp_payload(user_id: int) -> dict:
    """Own connection — this runs on the web thread, not the bot's."""
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
        credited = set()
        for r in rows:
            if not r["clock_out"]:
                open_count += 1
                continue
            a = datetime.fromisoformat(r["clock_in"])
            b = datetime.fromisoformat(r["clock_out"])
            b_min, ot_min = entry_split(r)
            mins = b_min + ot_min
            if PAY_MODE == "slot" and r["slot_id"]:
                key = (r["the_date"], r["slot_id"])
                if key in credited:
                    continue          # same block, already counted
                credited.add(key)
            day = date.fromisoformat(r["the_date"])
            cents = round(mins / 60 * rate_on(day)) if mins else 0
            total_min += mins
            total_cents += cents
            seen_days.add(day)
            shifts.append({
                "date": day.strftime("%a %-d %b"),
                "times": f"{a.strftime('%H:%M')}\u2013{b.strftime('%H:%M')}",
                "hours": hhmm(mins),
                "ot": hhmm(ot_min) if ot_min else "",
                "pay": money(cents) if cents else "",
                "flagged": r["status"] == "auto",
            })
        rate = rate_on(today)
    finally:
        conn.close()

    shifts.reverse()
    return {
        "isAdmin": is_admin(user_id),
        "name": name,
        "month": first.strftime("%B %Y"),
        "hours": hhmm(total_min),
        "days": len(seen_days),
        "count": len(shifts),
        "showPay": bool(rate) and not is_salaried(user_id),
        "salaried": is_salaried(user_id),
        "rate": money(rate),
        "total": money(total_cents),
        "openShift": bool(open_count),
        "shifts": shifts,
    }


def team_payload(first: date, mode: str = "month") -> dict:
    """Everyone's hours for a week or a month, for the admin view."""
    if mode == "week":
        first = first - timedelta(days=first.weekday())
        last = first + timedelta(days=6)
        span = (
            f"{first.strftime('%-d')}–{last.strftime('%-d %b')}"
            if first.month == last.month
            else f"{first.strftime('%-d %b')} – {last.strftime('%-d %b')}"
        )
        label = f"{quarter_week(first)} · {span}"
        prev_s = (first - timedelta(days=7)).isoformat()
        next_s = (first + timedelta(days=7)).isoformat()
    else:
        first = first.replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        label = first.strftime("%B %Y")
        prev_s = (first - timedelta(days=1)).replace(day=1).isoformat()
        next_s = (last + timedelta(days=1)).isoformat()

    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        agents = conn.execute(
            "SELECT user_id, display_name, name FROM agents WHERE status='active' "
            "ORDER BY name"
        ).fetchall()
    finally:
        conn.close()

    rows, tot_min, tot_ot, tot_cents, tot_rev, flags = [], 0, 0, 0, 0, 0
    for a in agents:
        t = timesheet(a["user_id"], first, last)
        revs = len(reviews_for(a["user_id"], first, last))
        rc = revs * REVIEW_RATE_CENTS
        if not t["shifts"] and not revs:
            continue
        odd = sum(
            1 for sh in t["shifts"]
            if sh["row"]["status"] == "auto" or not sh["row"]["slot_id"]
        )
        flags += odd
        tot_min += t["minutes"]
        tot_ot += t.get("overtime", 0)
        tot_cents += t["cents"] + rc
        tot_rev += revs
        rows.append({
            "name": a["display_name"] or a["name"],
            "shifts": len(t["shifts"]),
            "minutes": t["minutes"],
            "hours": hhmm(t["minutes"]),
            "ot": hhmm(t.get("overtime", 0)) if t.get("overtime") else "",
            "reviews": revs,
            "pay": "salaried" if is_salaried(a["user_id"]) else money(t["cents"] + rc),
            "flags": odd,
        })
    rows.sort(key=lambda r: -r["minutes"])
    top = rows[0]["minutes"] if rows else 1
    for r in rows:
        r["bar"] = round(r["minutes"] / top * 100) if top else 0

    return {
        "mode": mode,
        "label": label,
        "prev": prev_s,
        "next": next_s,
        "hasNext": last < now().date(),
        "people": rows,
        "totalHours": hhmm(tot_min),
        "totalOt": hhmm(tot_ot) if tot_ot else "",
        "totalPay": money(tot_cents),
        "totalReviews": tot_rev,
        "flags": flags,
        "headcount": len(rows),
    }


def home_payload(user_id: int) -> dict:
    """What an agent needs to see mid-shift."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        a = conn.execute(
            "SELECT display_name, name FROM agents WHERE user_id=?", (user_id,)
        ).fetchone()
        name = (a["display_name"] or a["name"]) if a else "You"
    finally:
        conn.close()

    today = now().date()
    openrow = q1(
        "SELECT * FROM time_entries WHERE agent_id=? AND clock_out IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    on_shift = bool(openrow)
    since = label = ""
    elapsed = ""
    if openrow:
        started = datetime.fromisoformat(openrow["clock_in"])
        since = started.strftime("%H:%M")
        label = openrow["shift_label"] or ""
        if not label and openrow["slot_id"]:
            r = q1("SELECT label FROM slots WHERE id=?", (openrow["slot_id"],))
            label = r["label"] if r else ""
        elapsed = hhmm(int((now() - started).total_seconds() // 60))

    # next rostered shift from now on
    nxt = q1(
        """SELECT s.label, s.start_min, d.name AS day_name, d.the_date
           FROM signups su JOIN slots s ON s.id = su.slot_id
           JOIN days d ON d.id = s.day_id
           WHERE su.user_id=? AND d.the_date >= ?
           ORDER BY d.the_date, s.start_min LIMIT 1""",
        (user_id, today.isoformat()),
    )
    next_shift = ""
    if nxt:
        d = date.fromisoformat(nxt["the_date"])
        when = "Today" if d == today else (
            "Tomorrow" if d == today + timedelta(days=1)
            else d.strftime("%a %-d %b")
        )
        next_shift = f"{when} · {nxt['label']}"

    first, last = week_bounds(today)
    t = timesheet(user_id, first, last)
    cases = open_cases()

    return {
        "name": name,
        "today": today.strftime("%A %-d %B"),
        "onShift": on_shift,
        "since": since,
        "elapsed": elapsed,
        "shiftLabel": label,
        "nextShift": next_shift,
        "weekHours": hhmm(t["minutes"]),
        "weekShifts": len(t["shifts"]),
        "openCases": len(cases),
        "cases": [
            {
                "id": c["id"],
                "prio": c["prio"] or "",
                "username": c["username"] or "",
                "platform": c["platform"] or "",
                "store": c["store"] or "",
                "flag": c["flag"] or "",
                "from": display_name_of(c["agent_id"], ""),
                "age": (today - date.fromisoformat(c["the_date"])).days,
            }
            for c in cases[:5]
        ],
        "support": support_for(user_id, name),
    }


def web_clock_in(user_id: int, label: str = "") -> dict:
    """Clock in from the app. Mirrors /clockin, minus the chat conversation."""
    if q1("SELECT 1 FROM time_entries WHERE agent_id=? AND clock_out IS NULL",
          (user_id,)):
        return {"ok": False, "error": "You're already clocked in."}

    when = now()
    slot = None
    if label:
        d = day_row_for(when.date().isoformat())
        slot = q1(
            "SELECT id, label FROM slots WHERE day_id=? AND lower(label)=lower(?)",
            (d["id"], label),
        ) if d else None
    else:
        slot = current_slot_for(user_id, when)

    shown = (slot["label"] if slot else label) or ""
    run(
        "INSERT INTO time_entries (agent_id, slot_id, the_date, clock_in, status,"
        " shift_label, opening_posted) VALUES (?,?,?,?,'open',?,?)",
        (user_id, slot["id"] if slot else None, when.date().isoformat(),
         when.isoformat(), shown, 1 if shown else 0),
    )

    posted = False
    if shown:
        nice = display_name_of(user_id, "")
        block = opening_block(user_id, nice, shown)
        posted = bool(on_bot_loop(post_ops(bot_ref(), block)))

    return {
        "ok": True, "at": when.strftime("%H:%M"),
        "shift": shown, "posted": posted,
        "needLabel": not shown,
    }


def web_clock_out(user_id: int) -> dict:
    """Clock out from the app."""
    row = q1(
        "SELECT * FROM time_entries WHERE agent_id=? AND clock_out IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    if not row:
        return {"ok": False, "error": "You're not clocked in."}

    when = now()
    run("UPDATE time_entries SET clock_out=?, status='closed' WHERE id=?",
        (when.isoformat(), row["id"]))
    fresh = q1("SELECT * FROM time_entries WHERE id=?", (row["id"],))
    base, ot = entry_split(fresh)
    cents = round((base + ot) / 60 * rate_for(user_id, date.fromisoformat(row["the_date"])))

    return {
        "ok": True, "at": when.strftime("%H:%M"),
        "shift": hhmm(base), "ot": hhmm(ot) if ot else "",
        "total": hhmm(base + ot),
        "pay": money(cents) if cents else "",
        "salaried": is_salaried(user_id),
    }


def todays_slots(user_id: int) -> list:
    """Slot labels for today, to pick from when the roster can't be matched."""
    d = day_row_for(now().date().isoformat())
    if not d:
        return []
    return [r["label"] for r in
            q("SELECT label FROM slots WHERE day_id=? ORDER BY idx", (d["id"],))]


def week_payload(user_id: int, which: str = "now") -> dict:
    """The board as data: every day, every slot, who's on it."""
    here = this_week()
    nxt = open_week()
    w = nxt if (which == "next" and nxt) else here
    if not w:
        return {"empty": True, "label": "", "days": []}

    # only offer the toggle when the two are actually different weeks
    other = None
    if nxt and here and nxt["id"] != here["id"]:
        other = "next" if which != "next" else "now"

    today = now().date()
    days = []
    for d in q("SELECT * FROM days WHERE week_id=? ORDER BY idx", (w["id"],)):
        the_date = date.fromisoformat(d["the_date"])
        slots = []
        for sl in q("SELECT * FROM slots WHERE day_id=? ORDER BY idx", (d["id"],)):
            holders = slot_holders(sl["id"])
            cap = sl["capacity"] or SLOT_CAPACITY
            mine = any(h["user_id"] == user_id for h in holders)
            names = [h["name"] for h in holders]
            slots.append({
                "id": sl["id"],
                "label": sl["label"],
                "mine": mine,
                "names": names,
                "taken": len(holders),
                "cap": cap,
                "full": len(holders) >= cap,
                "past": the_date < today,
            })
        days.append({
            "name": d["name"],
            "date": the_date.strftime("%-d %b"),
            "today": the_date == today,
            "past": the_date < today,
            "slots": slots,
        })

    mine_count = sum(
        1 for d in days for sl in d["slots"] if sl["mine"]
    )
    gaps = sum(1 for d in days for sl in d["slots"]
               if not sl["full"] and not sl["past"])

    deadline = ""
    if w["deadline"]:
        try:
            deadline = datetime.fromisoformat(w["deadline"]).strftime("%a %H:%M")
        except ValueError:
            deadline = ""

    return {
        "empty": False,
        "label": w["label"],
        "open": w["status"] == "open",
        "deadline": deadline,
        "which": which,
        "other": other,
        "otherLabel": (nxt["label"] if other == "next" else
                       (here["label"] if other == "now" else "")),
        "days": days,
        "mine": mine_count,
        "gaps": gaps,
    }


def web_claim(user_id: int, slot_id: int, want: bool) -> dict:
    """Take or release a slot from the app.

    The server decides, not the screen — two people tapping the same free slot
    can't both win.
    """
    slot = q1(
        """SELECT s.id, s.label, s.capacity, d.id AS day_id, d.name AS day_name,
                  d.the_date, w.id AS week_id, w.status
           FROM slots s JOIN days d ON d.id = s.day_id
           JOIN weeks w ON w.id = d.week_id WHERE s.id=?""",
        (slot_id,),
    )
    if not slot:
        return {"ok": False, "error": "That slot no longer exists."}
    if slot["status"] != "open":
        return {"ok": False, "error":
                "That week is closed. Use /pickup to ask for a shift."}
    if date.fromisoformat(slot["the_date"]) < now().date():
        return {"ok": False, "error": "That day has already passed."}
    if is_locked(slot["week_id"], user_id):
        return {"ok": False, "error":
                "You've confirmed your week. Unlock it in /plan to change it."}

    holders = slot_holders(slot["id"])
    mine = any(h["user_id"] == user_id for h in holders)
    cap = slot["capacity"] or SLOT_CAPACITY

    if want and mine:
        return {"ok": True, "state": "mine", "quiet": True}
    if want and len(holders) >= cap:
        who = ", ".join(h["name"] for h in holders)
        return {"ok": False, "error": f"{who} got there first.", "refresh": True}
    if not want and not mine:
        return {"ok": True, "state": "free", "quiet": True}

    name = display_name_of(user_id, "")
    if want:
        run(
            "INSERT OR IGNORE INTO signups (slot_id, user_id, name, ts) "
            "VALUES (?,?,?,?)",
            (slot["id"], user_id, name, now().isoformat()),
        )
    else:
        run("DELETE FROM signups WHERE slot_id=? AND user_id=?",
            (slot["id"], user_id))

    # redraw the group board through the bot
    on_bot_loop(refresh_group_for(slot["week_id"], slot["day_id"]))

    return {
        "ok": True,
        "state": "mine" if want else "free",
        "label": f"{slot['day_name']} {slot['label']}",
    }


def handover_payload(user_id: int) -> dict:
    """Open cases, recently closed ones, and the pickers for a new case."""
    today = now().date()
    open_rows = q("SELECT * FROM ho_cases WHERE closed=0 ORDER BY id")
    closed_rows = q(
        "SELECT * FROM ho_cases WHERE closed=1 ORDER BY closed_at DESC LIMIT 12"
    )

    def shape(r, closed=False):
        age = (today - date.fromisoformat(r["the_date"])).days
        out = {
            "id": r["id"],
            "prio": r["prio"] or "",
            "username": r["username"] or "",
            "platform": r["platform"] or "",
            "store": r["store"] or "",
            "flag": r["flag"] or "",
            "section": r["section"] or "open",
            "body": r["body"] or "",
            "from": display_name_of(r["agent_id"], ""),
            "age": age,
            "stale": age >= 3,
        }
        if closed:
            out["closedBy"] = display_name_of(r["closed_by"], "") if r["closed_by"] else ""
            when = r["closed_at"]
            out["closedWhen"] = (
                datetime.fromisoformat(when).strftime("%-d %b %H:%M") if when else ""
            )
        return out

    return {
        "openCases": [shape(r) for r in open_rows],
        "closedCases": [shape(r, True) for r in closed_rows],
        "stores": [{"flag": f, "store": st} for f, st in STORES],
        "platforms": PLATFORMS,
        "priorities": [{"emoji": e, "name": n} for e, n in PRIORITIES],
        "nextWorking": next_working_day(today).strftime("%A %-d %b"),
        "support": support_for(user_id, display_name_of(user_id, "")),
    }


def web_case_toggle(user_id: int, case_id: int, close: bool) -> dict:
    row = q1("SELECT * FROM ho_cases WHERE id=?", (case_id,))
    if not row:
        return {"ok": False, "error": "That case is gone."}
    if close:
        run("UPDATE ho_cases SET closed=1, closed_by=?, closed_at=? WHERE id=?",
            (user_id, now().isoformat(), case_id))
    else:
        run("UPDATE ho_cases SET closed=0, closed_by=NULL, closed_at=NULL "
            "WHERE id=?", (case_id,))
    return {"ok": True, "closed": close, "username": row["username"]}


def web_case_add(user_id: int, c: dict) -> dict:
    username = str(c.get("username", "")).strip()
    body = str(c.get("body", "")).strip()
    if not username:
        return {"ok": False, "error": "Who's the customer?"}
    if len(body) < 5:
        return {"ok": False, "error": "Add a bit more detail to the case."}

    platform = str(c.get("platform", "")).strip().upper()
    if platform not in PLATFORMS:
        return {"ok": False, "error": "Pick Duoke or Livechat."}
    prio = str(c.get("prio", "")).strip()
    if prio not in [e for e, _ in PRIORITIES]:
        return {"ok": False, "error": "Pick how urgent it is."}
    section = "follow" if c.get("section") == "follow" else "open"

    flag = store = ""
    if platform == "DUOKE":
        want = str(c.get("store", "")).strip()
        match = [(f, st) for f, st in STORES if st == want]
        if not match:
            return {"ok": False, "error": "Pick a store."}
        flag, store = match[0]

    run(
        "INSERT INTO ho_cases (agent_id, the_date, section, prio, platform, flag,"
        " store, username, body, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, now().date().isoformat(), section, prio, platform, flag,
         store, username, body, now().isoformat()),
    )
    return {"ok": True, "username": username}


def web_handover_post(user_id: int) -> dict:
    """Post whatever is open, noting what this person closed today."""
    today = now().date()
    cases = [case_row_to_dict(r) for r in
             q("SELECT * FROM ho_cases WHERE closed=0 ORDER BY id")]
    closed = [
        case_row_to_dict(r) for r in q(
            "SELECT * FROM ho_cases WHERE closed=1 AND closed_by=? "
            "AND closed_at >= ? ORDER BY closed_at",
            (user_id, today.isoformat()),
        )
    ]
    who = display_name_of(user_id, "")
    text = render_handover(cases, who, today, closed)

    run(
        "INSERT INTO handovers (agent_id, the_date, body, created_at) "
        "VALUES (?,?,?,?)",
        (user_id, today.isoformat(),
         "\n\n".join(render_case(c) for c in cases), now().isoformat()),
    )
    posted = bool(on_bot_loop(post_ops(bot_ref(), text)))
    return {
        "ok": True, "posted": posted, "open": len(cases), "closed": len(closed),
        "text": text if not posted else "",
    }


def web_has_access(user_id: int) -> bool:
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
            log.warning("Mini App handler error: %s", e)
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
        if path == "/api/handover":
            try:
                user = verify_init_data(self.headers.get("X-Init-Data", ""))
                if not user or "id" not in user:
                    self._send(401, b'{"error":"unverified"}')
                    return
                uid = int(user["id"])
                if not web_has_access(uid):
                    self._send(403, b'{"error":"no access"}')
                    return
                self._send(200, json.dumps(handover_payload(uid)).encode())
            except Exception as e:
                log.warning("Handover view failed: %s", e)
                try:
                    self._send(500, b'{"error":"server"}')
                except Exception:
                    pass
            return
        if path == "/api/week":
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                user = verify_init_data(self.headers.get("X-Init-Data", ""))
                if not user or "id" not in user:
                    self._send(401, b'{"error":"unverified"}')
                    return
                uid = int(user["id"])
                if not web_has_access(uid):
                    self._send(403, b'{"error":"no access"}')
                    return
                which = (qs.get("which") or ["now"])[0]
                self._send(200, json.dumps(week_payload(uid, which)).encode())
            except Exception as e:
                log.warning("Week view failed: %s", e)
                try:
                    self._send(500, b'{"error":"server"}')
                except Exception:
                    pass
            return
        if path == "/api/home":
            try:
                user = verify_init_data(self.headers.get("X-Init-Data", ""))
                if not user or "id" not in user:
                    self._send(401, b'{"error":"unverified"}')
                    return
                uid = int(user["id"])
                if not web_has_access(uid):
                    self._send(403, b'{"error":"no access"}')
                    return
                data = home_payload(uid)
                data["slotsToday"] = todays_slots(uid)
                data["isAdmin"] = is_admin(uid)
                self._send(200, json.dumps(data).encode())
            except Exception as e:
                log.warning("Home view failed: %s", e)
                try:
                    self._send(500, b'{"error":"server"}')
                except Exception:
                    pass
            return
        if path == "/api/team":
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                user = verify_init_data(self.headers.get("X-Init-Data", ""))
                if not user or "id" not in user:
                    self._send(401, b'{"error":"unverified"}')
                    return
                uid = int(user["id"])
                if not is_admin(uid):
                    self._send(403, b'{"error":"admins only"}')
                    return
                mode = (qs.get("mode") or ["week"])[0]
                mode = "week" if mode == "week" else "month"
                raw = (qs.get("start") or [""])[0]
                try:
                    first = date.fromisoformat(raw)
                except ValueError:
                    first = now().date()
                self._send(200, json.dumps(team_payload(first, mode)).encode())
            except Exception as e:
                log.warning("Team view failed: %s", e)
                try:
                    self._send(500, b'{"error":"server"}')
                except Exception:
                    pass
            return
        if path == "/api/me":
            try:
                user = verify_init_data(self.headers.get("X-Init-Data", ""))
                if not user or "id" not in user:
                    self._send(401, b'{"error":"unverified"}')
                    return
                uid = int(user["id"])
                if not web_has_access(uid):
                    self._send(403, b'{"error":"no access"}')
                    return
                self._send(200, json.dumps(miniapp_payload(uid)).encode())
            except Exception as e:
                log.warning("Mini App request failed: %s", e)
                try:
                    self._send(500, b'{"error":"server"}')
                except Exception:
                    pass
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        try:
            self._post()
        except Exception as e:
            log.warning("Mini App POST failed: %s", e)
            try:
                self._send(500, b'{"error":"server"}')
            except Exception:
                pass

    def _post(self):
        path = urllib.parse.urlparse(self.path).path
        user = verify_init_data(self.headers.get("X-Init-Data", ""))
        if not user or "id" not in user:
            self._send(401, b'{"error":"unverified"}')
            return
        uid = int(user["id"])
        if not web_has_access(uid):
            self._send(403, b'{"error":"no access"}')
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}

        if path == "/api/case":
            out = web_case_add(uid, body if isinstance(body, dict) else {})
        elif path == "/api/case/toggle":
            try:
                cid = int(body.get("id", 0))
            except (TypeError, ValueError):
                cid = 0
            out = (web_case_toggle(uid, cid, bool(body.get("close", True)))
                   if cid else {"ok": False, "error": "No case given."})
        elif path == "/api/handover/post":
            out = web_handover_post(uid)
        elif path == "/api/claim":
            try:
                sid = int(body.get("slot", 0))
            except (TypeError, ValueError):
                sid = 0
            async_safe = bool(body.get("want", True))
            out = web_claim(uid, sid, async_safe) if sid else {
                "ok": False, "error": "No slot given."}
        elif path == "/api/clockin":
            out = web_clock_in(uid, str(body.get("label", "")).strip())
        elif path == "/api/clockout":
            out = web_clock_out(uid)
        else:
            self._send(404, b'{"error":"not found"}')
            return
        self._send(200, json.dumps(out).encode())

    def log_message(self, *a):
        pass


def start_web_server(app=None) -> None:
    if app is not None:
        set_bot_handle(app)
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), MiniAppHandler)
    except Exception as e:
        log.warning("Mini App server could not start: %s", e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Mini App server listening on port %s", WEB_PORT)
