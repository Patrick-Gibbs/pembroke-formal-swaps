"""DB-facing operations: ballot runs, cancellation, slot release, atomic claims.

Capacity model: a formal's claimable seats =
    slots - active allocations - pending holds
where a "pending hold" is a released_slots row whose release_at is still in
the future and which hasn't been claimed. Cancelling therefore frees the seat
only once its randomized release_at passes.
"""
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config
from .allocation import run_ballot
from .db import immediate, audit, get_setting

LONDON = ZoneInfo(config.TIMEZONE)


def utcnow_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def local_now():
    return datetime.now(LONDON).replace(tzinfo=None)


def parse_local(s):
    """'YYYY-MM-DD HH:MM' (or with :SS / T separator) in Europe/London."""
    return datetime.fromisoformat(s.replace("T", " "))


def hours_until_formal(formal_dt_str):
    return (parse_local(formal_dt_str) - local_now()).total_seconds() / 3600


def cancel_cutoff_hours(conn):
    """Hours before a formal at which cancellations (and claims) close.
    Admin-editable setting; falls back to config.CANCEL_CUTOFF_HOURS."""
    try:
        return float(get_setting(conn, "cancel_cutoff_hours", "")
                     or config.CANCEL_CUTOFF_HOURS)
    except ValueError:
        return config.CANCEL_CUTOFF_HOURS


def pending_holds(conn, formal_id):
    now = utcnow_str()
    return conn.execute(
        "SELECT COUNT(*) AS n FROM released_slots "
        "WHERE formal_id=? AND claimed_by IS NULL AND release_at > ?",
        (formal_id, now)).fetchone()["n"]


def active_count(conn, formal_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM allocations WHERE formal_id=? AND status='active'",
        (formal_id,)).fetchone()["n"]


def free_seats(conn, formal_id, slots=None):
    if slots is None:
        slots = conn.execute("SELECT slots FROM formals WHERE id=?",
                             (formal_id,)).fetchone()["slots"]
    return max(0, slots - active_count(conn, formal_id) - pending_holds(conn, formal_id))


# ------------------------------------------------------------------ ballot

def run_allocation(conn, term, seed=None, actor="admin"):
    """Run the ballot for every open formal in `term`. Existing active
    allocations keep their seats; the ballot fills remaining capacity for
    users without a seat at that formal.
    Returns (run_id, seed, places, log, new_allocs)."""
    seed = seed or secrets.token_hex(8)
    with immediate(conn):
        formals = conn.execute(
            "SELECT id, slots FROM formals WHERE term=? AND status IN ('open','allocated')",
            (term,)).fetchall()
        capacity = {}
        for f in formals:
            now = utcnow_str()
            holds = conn.execute(
                "SELECT COUNT(*) AS n FROM released_slots "
                "WHERE formal_id=? AND claimed_by IS NULL AND release_at > ?",
                (f["id"], now)).fetchone()["n"]
            taken = conn.execute(
                "SELECT COUNT(*) AS n FROM allocations WHERE formal_id=? AND status='active'",
                (f["id"],)).fetchone()["n"]
            capacity[f["id"]] = max(0, f["slots"] - taken - holds)

        raw_prefs = {}
        rows = conn.execute(
            "SELECT p.user_id, p.formal_id FROM preferences p "
            "JOIN users u ON u.id = p.user_id AND u.email_verified = 1 "
            "WHERE p.term = ? ORDER BY p.user_id, p.rank", (term,)).fetchall()
        for r in rows:
            raw_prefs.setdefault(r["user_id"], []).append(r["formal_id"])
        held = conn.execute(
            "SELECT user_id, formal_id FROM allocations WHERE status='active'").fetchall()
        held_set = {(h["user_id"], h["formal_id"]) for h in held}

        # Personal caps: "max swaps I'm happy to be assigned" (1-3, default 3).
        # Applies to the ballot ONLY: it limits how many formals this run can
        # hand someone, but never blocks later claims of released slots (a
        # claim is a deliberate act) and ignores places gained outside the
        # ballot. A group is limited by its most-constrained accepted member.
        cap_rows = {r["user_id"]: r["max_places"] for r in conn.execute(
            "SELECT user_id, max_places FROM ballot_caps WHERE term=?",
            (term,)).fetchall()}

        def remaining_cap(user_id):
            return cap_rows.get(user_id, 3)

        # Balloting units: a group (accepted members, leader's ranking, block
        # size = member count) or a solo entrant. A formal any member already
        # attends is dropped from the unit's list, and nobody may get a second
        # seat at a formal they already hold.
        unit_prefs, unit_sizes, unit_members = {}, {}, {}
        unit_caps = {}
        grouped_users = set()
        for g in conn.execute("SELECT id, leader_user_id FROM ballot_groups "
                              "WHERE term=?", (term,)).fetchall():
            members = [m["user_id"] for m in conn.execute(
                "SELECT user_id FROM ballot_group_members WHERE group_id=? "
                "AND status='accepted' ORDER BY user_id", (g["id"],)).fetchall()]
            if not members:
                continue
            grouped_users.update(members)
            plist = [f for f in raw_prefs.get(g["leader_user_id"], [])
                     if all((m, f) not in held_set for m in members)]
            if plist:
                uid = f"g:{g['id']}"
                unit_prefs[uid] = plist
                unit_sizes[uid] = len(members)
                unit_members[uid] = members
                unit_caps[uid] = min(remaining_cap(m) for m in members)
        for u, plist in raw_prefs.items():
            if u in grouped_users:
                continue  # a group member's personal ranking is inert
            plist = [f for f in plist if (u, f) not in held_set]
            if plist:
                unit_prefs[f"u:{u}"] = plist
                unit_sizes[f"u:{u}"] = 1
                unit_members[f"u:{u}"] = [u]
                unit_caps[f"u:{u}"] = remaining_cap(u)

        assignments, log = run_ballot(capacity, unit_prefs, seed, unit_sizes,
                                      unit_caps)

        new_allocs = []  # (user_id, formal_id) inserted by this run
        for uid, fid, _rnd in assignments:
            for member in unit_members[uid]:
                conn.execute(
                    "INSERT INTO allocations(user_id, formal_id, status, source) "
                    "VALUES (?,?,'active','ballot')", (member, fid))
                new_allocs.append((member, fid))
        placed = len(new_allocs)
        conn.execute("UPDATE formals SET status='allocated' WHERE term=? AND status='open'",
                     (term,))
        summary = (f"{placed} places assigned to {len(assignments)} unit-formal "
                   f"pairs; {len(log)} lotteries")
        cur = conn.execute("INSERT INTO allocation_runs(term, seed, summary) VALUES (?,?,?)",
                           (term, seed, summary + "\n" + "\n".join(log)))
        audit(conn, actor, "run_allocation", f"term={term} seed={seed} {summary}")
        return cur.lastrowid, seed, placed, log, new_allocs


# ------------------------------------------------------------------ cancel / release

class CancelError(Exception):
    pass


def cancel_allocation(conn, user_id, allocation_id, rng=None, actor=None):
    """Cancel an active allocation; hold the seat for a random 0-60 min."""
    rng = rng or secrets.SystemRandom()
    with immediate(conn):
        alloc = conn.execute(
            "SELECT a.*, f.dt, f.host_college FROM allocations a "
            "JOIN formals f ON f.id = a.formal_id WHERE a.id=? AND a.status='active'",
            (allocation_id,)).fetchone()
        if alloc is None or (user_id is not None and alloc["user_id"] != user_id):
            raise CancelError("No such active booking.")
        cutoff = cancel_cutoff_hours(conn)
        if user_id is not None and hours_until_formal(alloc["dt"]) < cutoff:
            raise CancelError(f"Cancellations close {int(cutoff)} hours "
                              "before the formal.")
        delay_s = rng.randint(0, 3600)
        release_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_s)
                      ).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE allocations SET status='cancelled', cancelled_at=? WHERE id=?",
                     (utcnow_str(), allocation_id))
        conn.execute(
            "INSERT INTO released_slots(formal_id, allocation_id, release_at) VALUES (?,?,?)",
            (alloc["formal_id"], allocation_id, release_at))
        audit(conn, actor or f"user:{alloc['user_id']}", "cancel",
              f"allocation={allocation_id} formal={alloc['formal_id']} release_at={release_at}Z")
        return release_at


class ClaimError(Exception):
    pass


def claim_seat(conn, user_id, formal_id, actor=None):
    """Atomically claim a free seat. BEGIN IMMEDIATE serializes writers, so
    the capacity check + insert are race-free; the partial unique index on
    active (user_id, formal_id) is a second line of defence."""
    with immediate(conn):
        f = conn.execute("SELECT * FROM formals WHERE id=? AND status IN ('open','allocated')",
                         (formal_id,)).fetchone()
        if f is None:
            raise ClaimError("This formal is not available.")
        cutoff = cancel_cutoff_hours(conn)
        if hours_until_formal(f["dt"]) < cutoff:
            raise ClaimError(f"Claims close {int(cutoff)} hours before the formal.")
        already = conn.execute(
            "SELECT 1 FROM allocations WHERE user_id=? AND formal_id=? AND status='active'",
            (user_id, formal_id)).fetchone()
        if already:
            raise ClaimError("You already have a place at this formal.")
        if free_seats(conn, formal_id, f["slots"]) < 1:
            raise ClaimError("Sorry — no free places (someone may have beaten you to it).")
        conn.execute(
            "INSERT INTO allocations(user_id, formal_id, status, source) "
            "VALUES (?,?,'active','claim')", (user_id, formal_id))
        # Mark one opened released slot as consumed, if one exists (a seat
        # may also be free simply because the ballot didn't fill it).
        row = conn.execute(
            "SELECT id FROM released_slots WHERE formal_id=? AND claimed_by IS NULL "
            "AND release_at <= ? ORDER BY release_at LIMIT 1",
            (formal_id, utcnow_str())).fetchone()
        if row:
            conn.execute("UPDATE released_slots SET claimed_by=?, claimed_at=? WHERE id=?",
                         (user_id, utcnow_str(), row["id"]))
        audit(conn, actor or f"user:{user_id}", "claim", f"formal={formal_id}")


def attendee_emails(conn, formal_id):
    return [r["email"] for r in conn.execute(
        "SELECT u.email FROM allocations a JOIN users u ON u.id = a.user_id "
        "WHERE a.formal_id=? AND a.status='active' AND u.email_verified=1",
        (formal_id,)).fetchall()]


def send_scheduled_emails(conn, send_reminder, send_review, now=None):
    """Day-of emails. Called periodically by the worker thread.

    - 09:00 UK on the day of a formal: courtesy reminder to every attendee
      (skipped entirely if the formal has already started when we first check,
      e.g. after prolonged downtime).
    - 21:00 UK on the day of the formal (and not before the formal's start):
      review request to every attendee.

    send_reminder(formal_row, [emails]) / send_review(formal_row, [emails])
    do the delivery. Flags are flipped inside an immediate transaction first,
    so emails go at most once even with overlapping calls."""
    now = now or local_now()
    fired = []
    rows = conn.execute(
        "SELECT id FROM formals WHERE status IN ('open','allocated') "
        "AND (reminder_sent=0 OR review_sent=0)").fetchall()
    for row in rows:
        f = conn.execute("SELECT * FROM formals WHERE id=?", (row["id"],)).fetchone()
        start = parse_local(f["dt"])
        if start.date() != now.date():
            # Reminder/review are strictly day-of; mark long-past formals done
            # so we stop scanning them.
            if start < now - timedelta(days=1):
                conn.execute("UPDATE formals SET reminder_sent=1, review_sent=1 "
                             "WHERE id=?", (f["id"],))
            continue
        nine = start.replace(hour=9, minute=0, second=0, microsecond=0)
        nine_pm = start.replace(hour=21, minute=0, second=0, microsecond=0)
        if not f["reminder_sent"] and nine <= now < start:
            with immediate(conn):
                fresh = conn.execute("SELECT reminder_sent FROM formals WHERE id=?",
                                     (f["id"],)).fetchone()
                if fresh["reminder_sent"]:
                    continue
                conn.execute("UPDATE formals SET reminder_sent=1 WHERE id=?", (f["id"],))
            send_reminder(f, attendee_emails(conn, f["id"]))
            fired.append(("reminder", f["id"]))
        if not f["review_sent"] and now >= max(nine_pm, start):
            with immediate(conn):
                fresh = conn.execute("SELECT review_sent FROM formals WHERE id=?",
                                     (f["id"],)).fetchone()
                if fresh["review_sent"]:
                    continue
                conn.execute("UPDATE formals SET review_sent=1 WHERE id=?", (f["id"],))
            send_review(f, attendee_emails(conn, f["id"]))
            fired.append(("review", f["id"]))
    return fired


# ------------------------------------------------------------------ release worker step

def open_due_releases(conn, notify):
    """Called periodically. For each released slot whose release_at has
    passed and which hasn't triggered notifications yet, email subscribers.
    `notify(formal_row, [emails])` does the sending. Returns #slots opened."""
    now = utcnow_str()
    due = conn.execute(
        "SELECT r.id, r.formal_id FROM released_slots r "
        "JOIN formals f ON f.id = r.formal_id "
        "WHERE r.release_at <= ? AND r.notified = 0 AND r.claimed_by IS NULL",
        (now,)).fetchall()
    opened = 0
    for r in due:
        with immediate(conn):
            fresh = conn.execute(
                "SELECT notified FROM released_slots WHERE id=?", (r["id"],)).fetchone()
            if fresh["notified"]:
                continue
            conn.execute("UPDATE released_slots SET notified=1, opened=1 WHERE id=?",
                         (r["id"],))
        formal = conn.execute("SELECT * FROM formals WHERE id=?", (r["formal_id"],)).fetchone()
        subs = conn.execute(
            "SELECT u.email FROM subscriptions s JOIN users u ON u.id = s.user_id "
            "WHERE s.formal_id=? AND u.email_verified=1 AND u.id NOT IN "
            "(SELECT user_id FROM allocations WHERE formal_id=? AND status='active')",
            (r["formal_id"], r["formal_id"])).fetchall()
        notify(formal, [s["email"] for s in subs])
        opened += 1
    return opened
