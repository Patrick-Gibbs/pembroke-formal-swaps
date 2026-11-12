"""Incoming swaps: guests another college sends to formal hall at Pembroke.

Flow: the admin sets how many places we host (on the paired formal, or a
stand-alone incoming swap) and emails the other college's contact a private
form link. They enter their guests + dietary and confirm — the list then locks
and goes to the Formal Hall team (contact and admin CC'd). If they haven't
submitted, reminders go at 9am three days before the deadline, on the deadline
day and two days after it (admin CC'd), then stop. The deadline is
`incoming_deadline_days` (default 10) before the visit.
"""
import secrets
from datetime import datetime, timedelta

from . import config
from .db import audit, get_setting, immediate
from .services import local_now, parse_local, utcnow_str

DEFAULT_DEADLINE_DAYS = 10
# reminder stage -> days relative to the deadline
REMINDER_OFFSETS = {1: -3, 2: 0, 3: 2}
REMINDER_HOUR = 9


class GuestListError(Exception):
    pass


def deadline_days(conn):
    try:
        return max(0, int(get_setting(conn, "incoming_deadline_days", "")
                          or DEFAULT_DEADLINE_DAYS))
    except ValueError:
        return DEFAULT_DEADLINE_DAYS


def deadline(conn, swap):
    """The date the guest list is due (a date, UK)."""
    return parse_local(swap["dt"]).date() - timedelta(days=deadline_days(conn))


def guest_link(token):
    return f"{config.SITE_URL}/guests/{token}"


def ensure_token(conn, sid):
    row = conn.execute("SELECT token FROM incoming_swaps WHERE id=?", (sid,)).fetchone()
    if row["token"]:
        return row["token"]
    token = secrets.token_urlsafe(18)
    conn.execute("UPDATE incoming_swaps SET token=? WHERE id=?", (token, sid))
    return token


def due_stage(conn, swap, now=None):
    """Highest reminder stage whose 9am has passed (0 = none yet)."""
    now = now or local_now()
    due = deadline(conn, swap)
    stage = 0
    for st, off in REMINDER_OFFSETS.items():
        at = datetime.combine(due + timedelta(days=off), datetime.min.time()).replace(
            hour=REMINDER_HOUR)
        if now >= at:
            stage = max(stage, st)
    return stage


def _signoff(conn):
    name = get_setting(conn, "admin_name", "").strip()
    return f"— {name}, Pembroke College" if name else "— Pembroke Formal Swaps"


def _contact_name(swap):
    return (swap["host_name"] or "").strip() or f"{swap['guest_college']} formals team"


def _ctx(conn, swap):
    from .dates import uk_date, uk_datetime
    return {"contact_name": _contact_name(swap), "guest_college": swap["guest_college"],
            "dt": uk_datetime(swap["dt"]), "tickets": swap["tickets"],
            "deadline": uk_date(deadline(conn, swap).isoformat()),
            "link": guest_link(ensure_token(conn, swap["id"])),
            "signoff": _signoff(conn)}


def send_link(conn, sid):
    """Email the contact their form link. Reminder stages already due are
    skipped, so a late invite isn't followed straight away by a reminder.
    Returns (ok, message)."""
    from . import emailer
    swap = conn.execute("SELECT * FROM incoming_swaps WHERE id=?", (sid,)).fetchone()
    if swap is None:
        return False, "No such incoming swap."
    if swap["submitted_at"]:
        return False, f"{swap['guest_college']} has already submitted their list."
    if swap["tickets"] < 1:
        return False, f"{swap['guest_college']}: set how many places we're hosting first."
    to = emailer.split_emails(swap["host_email"])
    if not to:
        return False, f"{swap['guest_college']}: no contact email."
    if not emailer.incoming_invite_email(to, _ctx(conn, swap)):
        return False, f"{swap['guest_college']}: the email failed — see the email log."
    conn.execute("UPDATE incoming_swaps SET link_sent_at=?, reminder_stage=? WHERE id=?",
                 (utcnow_str(), due_stage(conn, swap), sid))
    audit(conn, "admin", "incoming_link_sent", f"swap={sid}")
    return True, f"{swap['guest_college']}: link sent to {', '.join(to)}."


def parse_guests(form, tickets):
    """[(first, last, dietary)] from the form's rows; blank rows are skipped."""
    guests = []
    for i in range(tickets):
        first = form.get(f"first_{i}", "").strip()[:80]
        last = form.get(f"last_{i}", "").strip()[:80]
        diet = form.get(f"dietary_{i}", "").strip()[:300]
        if not (first or last or diet):
            continue
        if not first or not last:
            raise GuestListError(f"Guest {i + 1}: please give a first and last name.")
        guests.append((first, last, diet))
    if not guests:
        raise GuestListError("Please enter at least one guest.")
    return guests


def _save(conn, sid, guests):
    """Replace a swap's guest list, keeping each kept guest's admin notes."""
    notes = {(r["first_name"].lower(), r["last_name"].lower()): r["notes"] for r in conn.execute(
        "SELECT first_name, last_name, notes FROM incoming_participants WHERE swap_id=?",
        (sid,))}
    conn.execute("DELETE FROM incoming_participants WHERE swap_id=?", (sid,))
    conn.executemany(
        "INSERT INTO incoming_participants(swap_id, first_name, last_name, dietary, notes) "
        "VALUES (?,?,?,?,?)",
        [(sid, f, l, d, notes.get((f.lower(), l.lower()), "")) for f, l, d in guests])


def admin_save(conn, sid, guests, submit=False):
    """Admin fills in / corrects a guest list (allowed even once submitted).
    submit=True also marks it submitted (stopping reminders) and emails the
    Formal Hall team. Returns (ok, message)."""
    with immediate(conn):
        _save(conn, sid, guests)
        if submit:
            conn.execute("UPDATE incoming_swaps SET submitted_at=COALESCE(submitted_at, ?) "
                         "WHERE id=?", (utcnow_str(), sid))
        audit(conn, "admin", "incoming_admin_save",
              f"swap={sid} guests={len(guests)} submit={int(submit)}")
    if not submit:
        return True, f"Saved {len(guests)} guest(s)."
    ok, msg = send_to_catering(conn, sid)
    return ok, f"Saved {len(guests)} guest(s) and submitted. " + msg


def submit(conn, token, guests):
    """Save and lock a contact's list, then email the Formal Hall team.
    Returns (swap row, catering ok). Raises GuestListError if it can't be
    submitted."""
    with immediate(conn):
        swap = conn.execute("SELECT * FROM incoming_swaps WHERE token=?",
                            (token,)).fetchone()
        if swap is None:
            raise GuestListError("This link isn't valid.")
        if swap["submitted_at"]:
            raise GuestListError("This guest list has already been submitted.")
        if len(guests) > swap["tickets"]:
            raise GuestListError(f"You have {swap['tickets']} place(s).")
        _save(conn, swap["id"], guests)
        conn.execute("UPDATE incoming_swaps SET submitted_at=? WHERE id=?",
                     (utcnow_str(), swap["id"]))
        audit(conn, "guest", "incoming_submit", f"swap={swap['id']} guests={len(guests)}")
    ok, _ = send_to_catering(conn, swap["id"])
    return swap, ok


def send_to_catering(conn, sid):
    """Email the guest list to the Formal Hall team, CC the contact and the
    admin. Returns (ok, message)."""
    from . import emailer
    swap = conn.execute("SELECT * FROM incoming_swaps WHERE id=?", (sid,)).fetchone()
    people = conn.execute("SELECT * FROM incoming_participants WHERE swap_id=? "
                          "ORDER BY last_name, first_name", (sid,)).fetchall()
    if not people:
        return False, "No guests to send."
    admin = get_setting(conn, "admin_email", "").strip()
    to = emailer.split_emails(get_setting(conn, "catering_email", ""))
    cc = [e for e in emailer.split_emails(swap["host_email"]) + ([admin] if admin else [])
          if e.lower() not in [t.lower() for t in to]]
    if not to:           # no Formal Hall address set: make sure the admin gets it
        if not admin:
            return False, "Set the Formal Hall team email in Settings."
        to, cc = [admin], [e for e in cc if e.lower() != admin.lower()]
    ok = emailer.incoming_catering_email(to, cc, swap, people, _signoff(conn))
    if ok:
        conn.execute("UPDATE incoming_swaps SET catering_sent_at=? WHERE id=?",
                     (utcnow_str(), sid))
    audit(conn, "system", "incoming_catering", f"swap={sid} to={to} ok={ok}")
    return ok, (f"Guest list sent to {', '.join(to)}." if ok
                else "The email to the Formal Hall team failed — see the email log.")


def send_due_reminders(conn, now=None):
    """Called by the background worker. Returns [(swap_id, stage)] sent."""
    from . import emailer
    now = now or local_now()
    fired = []
    for swap in conn.execute(
            "SELECT * FROM incoming_swaps WHERE submitted_at IS NULL AND tickets > 0 "
            "AND link_sent_at IS NOT NULL AND reminder_stage < 3").fetchall():
        if now >= parse_local(swap["dt"]):
            continue
        stage = due_stage(conn, swap, now)
        if stage <= swap["reminder_stage"]:
            continue
        with immediate(conn):
            fresh = conn.execute("SELECT reminder_stage, submitted_at FROM incoming_swaps "
                                 "WHERE id=?", (swap["id"],)).fetchone()
            if fresh["submitted_at"] or fresh["reminder_stage"] >= stage:
                continue
            conn.execute("UPDATE incoming_swaps SET reminder_stage=? WHERE id=?",
                         (stage, swap["id"]))
        to = emailer.split_emails(swap["host_email"])
        if not to:
            continue
        ctx = _ctx(conn, swap)
        ctx["due_phrase"] = {1: f"is due in 3 days, on <b>{ctx['deadline']}</b>",
                             2: "is due <b>today</b>",
                             3: f"was due on <b>{ctx['deadline']}</b>"}[stage]
        admin = get_setting(conn, "admin_email", "").strip()
        emailer.incoming_reminder_email(to, admin or None, ctx)
        fired.append((swap["id"], stage))
    return fired


def sync_from_formal(conn, fid, tickets, visit_dt):
    """Create/update the incoming swap paired with formal `fid` from its form:
    `tickets` places we host on `visit_dt`. Contact details follow the formal's
    host contact. tickets 0 leaves an existing pairing with 0 places."""
    f = conn.execute("SELECT * FROM formals WHERE id=?", (fid,)).fetchone()
    swap = conn.execute("SELECT * FROM incoming_swaps WHERE formal_id=?", (fid,)).fetchone()
    if swap is None:
        if tickets < 1:
            return
        conn.execute(
            "INSERT INTO incoming_swaps(guest_college, dt, host_name, host_email, "
            "host_phone, formal_id, tickets) VALUES (?,?,?,?,?,?,?)",
            (f["host_college"], visit_dt, f["host_name"], f["host_email"],
             f["host_phone"], fid, tickets))
        return
    conn.execute(
        "UPDATE incoming_swaps SET guest_college=?, dt=COALESCE(NULLIF(?, ''), dt), "
        "host_name=?, host_email=?, host_phone=?, tickets=? WHERE id=?",
        (f["host_college"], visit_dt, f["host_name"], f["host_email"], f["host_phone"],
         tickets, swap["id"]))
