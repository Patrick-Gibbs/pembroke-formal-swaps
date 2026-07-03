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
from .db import immediate, audit

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
    users without a seat at that formal. Returns (run_id, seed, assignments, log)."""
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

        prefs = {}
        rows = conn.execute(
            "SELECT p.user_id, p.formal_id FROM preferences p "
            "JOIN users u ON u.id = p.user_id AND u.email_verified = 1 "
            "WHERE p.term = ? ORDER BY p.user_id, p.rank", (term,)).fetchall()
        for r in rows:
            prefs.setdefault(r["user_id"], []).append(r["formal_id"])
        # Users already holding an active seat at a formal must not get a
        # second seat there: pass holdings in by pre-filtering their list.
        held = conn.execute(
            "SELECT user_id, formal_id FROM allocations WHERE status='active'").fetchall()
        held_set = {(h["user_id"], h["formal_id"]) for h in held}
        prefs = {uid: [f for f in fl if (uid, f) not in held_set]
                 for uid, fl in prefs.items()}

        assignments, log = run_ballot(capacity, prefs, seed)

        for uid, fid, _rnd in assignments:
            conn.execute(
                "INSERT INTO allocations(user_id, formal_id, status, source) "
                "VALUES (?,?,'active','ballot')", (uid, fid))
        conn.execute("UPDATE formals SET status='allocated' WHERE term=? AND status='open'",
                     (term,))
        summary = f"{len(assignments)} places assigned; {len(log)} lotteries"
        cur = conn.execute("INSERT INTO allocation_runs(term, seed, summary) VALUES (?,?,?)",
                           (term, seed, summary + "\n" + "\n".join(log)))
        audit(conn, actor, "run_allocation", f"term={term} seed={seed} {summary}")
        return cur.lastrowid, seed, assignments, log


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
        if user_id is not None and hours_until_formal(alloc["dt"]) < config.CANCEL_CUTOFF_HOURS:
            raise CancelError("Cancellations close 24 hours before the formal.")
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
        if hours_until_formal(f["dt"]) < config.CANCEL_CUTOFF_HOURS:
            raise ClaimError("Claims close 24 hours before the formal.")
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
