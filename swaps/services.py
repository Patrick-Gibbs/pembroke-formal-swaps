"""DB-facing operations: ballot runs, cancellation, slot release, atomic claims.

Capacity model: a formal's claimable seats =
    slots - active allocations - pending holds
where a "pending hold" is a released_slots row whose release_at is still in
the future and which hasn't been claimed. Cancelling therefore frees the seat
only once its randomized release_at passes.
"""
import math
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


def swap_cutoff_hours(conn):
    """Hours before a formal at which peer-to-peer swaps close. Admin-editable;
    falls back to config.SWAP_CUTOFF_HOURS (a week)."""
    try:
        return float(get_setting(conn, "swap_cutoff_hours", "")
                     or config.SWAP_CUTOFF_HOURS)
    except ValueError:
        return config.SWAP_CUTOFF_HOURS


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

def build_ballot_inputs(conn, term):
    """Assemble the exact inputs the ballot runs on, from current DB state.
    Returns (capacity, unit_prefs, unit_sizes, unit_caps, unit_members).
    Shared by run_allocation (real) and simulate_user (read-only preview) so
    the two can never drift apart."""
    formals = conn.execute(
        "SELECT id, slots FROM formals WHERE term=? AND status IN ('open','allocated')",
        (term,)).fetchall()
    capacity = {}
    now = utcnow_str()
    for f in formals:
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
    # Applies to the ballot ONLY: it limits how many formals this run can hand
    # someone, but never blocks later claims of released slots. A group uses the
    # leader's cap.
    cap_rows = {r["user_id"]: r["max_places"] for r in conn.execute(
        "SELECT user_id, max_places FROM ballot_caps WHERE term=?",
        (term,)).fetchall()}

    def remaining_cap(user_id):
        return cap_rows.get(user_id, 3)

    # Balloting units: a group (accepted members, leader's ranking, block size =
    # member count) or a solo entrant. A formal any member already attends is
    # dropped from the unit's list.
    unit_prefs, unit_sizes, unit_members, unit_caps = {}, {}, {}, {}
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
            unit_caps[uid] = remaining_cap(g["leader_user_id"])
    for u, plist in raw_prefs.items():
        if u in grouped_users:
            continue  # a group member's personal ranking is inert
        plist = [f for f in plist if (u, f) not in held_set]
        if plist:
            unit_prefs[f"u:{u}"] = plist
            unit_sizes[f"u:{u}"] = 1
            unit_members[f"u:{u}"] = [u]
            unit_caps[f"u:{u}"] = remaining_cap(u)
    return capacity, unit_prefs, unit_sizes, unit_caps, unit_members


def unit_id_for_user(unit_members, user_id):
    for uid, members in unit_members.items():
        if user_id in members:
            return uid
    return None


Z_95 = 1.96


def _wilson(k, n, z=Z_95):
    """95% Wilson score interval for a binomial proportion, as integer
    percentages. Well-behaved at the extremes (k=0 or k=n) unlike the normal
    approximation. Returns (lo_pct, hi_pct)."""
    if n == 0:
        return 0, 0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    lo = max(0.0, centre - half)
    hi = min(1.0, centre + half)
    return round(100 * lo), round(100 * hi)


def simulate_user(conn, user_id, term, trials=400):
    """Run the ballot `trials` times on the CURRENT state (no writes) and
    report this user's outcomes with 95% confidence intervals (Monte-Carlo
    error from the finite number of runs). Returns a dict, or None if the user
    isn't a live entrant (no viable ranked formals / not in an entered group)."""
    capacity, unit_prefs, unit_sizes, unit_caps, unit_members = \
        build_ballot_inputs(conn, term)
    uid = unit_id_for_user(unit_members, user_id)
    if uid is None:
        return None

    # The user's "first preference" = the first formal on the ranking that
    # governs their unit (their own if solo; the leader's if in a group).
    first_pref = unit_prefs[uid][0]
    college = {r["id"]: r["host_college"] for r in conn.execute(
        "SELECT id, host_college FROM formals WHERE term=?", (term,)).fetchall()}
    cap = min(3, unit_caps.get(uid, 3))  # how many swap slots this unit can win

    first_hits = 0
    total_swaps = 0
    total_sq = 0
    # slot_counts[k][fid] = # trials the unit won `fid` as its (k+1)-th swap
    # (its round-(k+1) win). fid None means "no swap in that slot".
    slot_counts = [{} for _ in range(cap)]
    for i in range(trials):
        assignments, _ = run_ballot(capacity, unit_prefs, f"sim-{i}",
                                    unit_sizes, unit_caps)
        by_round = {rnd: fid for u, fid, rnd in assignments if u == uid}
        won = set(by_round.values())
        if first_pref in won:
            first_hits += 1
        total_swaps += len(won)
        total_sq += len(won) ** 2
        for k in range(cap):
            fid = by_round.get(k + 1)  # round k+1 = the (k+1)-th swap
            slot_counts[k][fid] = slot_counts[k].get(fid, 0) + 1

    slots = []
    for k in range(cap):
        counts = slot_counts[k]
        options = []
        for fid, n in counts.items():
            if fid is None or not n:
                continue
            lo, hi = _wilson(n, trials)
            options.append({"college": college.get(fid, "?"),
                            "pct": round(100 * n / trials), "lo": lo, "hi": hi})
        options.sort(key=lambda o: -o["pct"])
        none_lo, none_hi = _wilson(counts.get(None, 0), trials)
        slots.append({
            "n": k + 1, "options": options,
            "none_pct": round(100 * counts.get(None, 0) / trials),
            "none_lo": none_lo, "none_hi": none_hi,
        })

    # 95% interval for the mean total (CLT; a bounded 0..cap count).
    mean = total_swaps / trials
    if trials > 1:
        var = max(0.0, (total_sq - trials * mean * mean) / (trials - 1))
        margin = Z_95 * math.sqrt(var) / math.sqrt(trials)
    else:
        margin = 0.0
    first_lo, first_hi = _wilson(first_hits, trials)
    return {
        "trials": trials,
        "in_group": uid.startswith("g:"),
        "first_pref_college": college.get(first_pref, ""),
        "first_pref_pct": round(100 * first_hits / trials),
        "first_pref_lo": first_lo, "first_pref_hi": first_hi,
        "expected_total": round(mean, 1),
        "expected_lo": round(max(0.0, mean - margin), 1),
        "expected_hi": round(min(float(cap), mean + margin), 1),
        "n_entrants": len(unit_members),
        "slots": slots,
    }


def simulate_pair(conn, user_id, other_id, term, trials=400):
    """Probability that `user_id` and `other_id` end up attending the same
    formal, over `trials` ballot runs on current state. Returns a dict, or None
    if the requesting user isn't a live entrant."""
    capacity, unit_prefs, unit_sizes, unit_caps, unit_members = \
        build_ballot_inputs(conn, term)
    a = unit_id_for_user(unit_members, user_id)
    if a is None:
        return None
    b = unit_id_for_user(unit_members, other_id)
    college = {r["id"]: r["host_college"] for r in conn.execute(
        "SELECT id, host_college FROM formals WHERE term=?", (term,)).fetchall()}
    other = conn.execute("SELECT first_name, last_name FROM users WHERE id=?",
                         (other_id,)).fetchone()
    other_name = f"{other['first_name']} {other['last_name']}" if other else "?"

    if b is None:  # the other person hasn't entered — can't share any formal
        return {"trials": trials, "other_name": other_name, "other_entered": False,
                "same_group": False, "together_pct": 0, "together_lo": 0,
                "together_hi": 0, "shared": []}
    if a == b:  # same ballot group — they always attend together
        r = simulate_user(conn, user_id, term, trials=trials)
        at_least_one = 100 - (r["slots"][0]["none_pct"] if r["slots"] else 100)
        return {"trials": trials, "other_name": other_name, "other_entered": True,
                "same_group": True, "together_pct": at_least_one,
                "together_lo": at_least_one, "together_hi": at_least_one,
                "shared": [{"college": o["college"], "pct": o["pct"],
                            "lo": o["lo"], "hi": o["hi"]}
                           for s in r["slots"] for o in s["options"]]}

    together = 0
    shared_counts = {}
    for i in range(trials):
        assignments, _ = run_ballot(capacity, unit_prefs, f"sim-{i}",
                                    unit_sizes, unit_caps)
        a_wins = {fid for u, fid, _ in assignments if u == a}
        b_wins = {fid for u, fid, _ in assignments if u == b}
        shared = a_wins & b_wins
        if shared:
            together += 1
        for fid in shared:
            shared_counts[fid] = shared_counts.get(fid, 0) + 1

    lo, hi = _wilson(together, trials)
    shared = sorted(
        ({"college": college.get(fid, "?"), "pct": round(100 * n / trials),
          "lo": _wilson(n, trials)[0], "hi": _wilson(n, trials)[1]}
         for fid, n in shared_counts.items() if n),
        key=lambda o: -o["pct"])
    return {"trials": trials, "other_name": other_name, "other_entered": True,
            "same_group": False, "together_pct": round(100 * together / trials),
            "together_lo": lo, "together_hi": hi, "shared": shared}


def run_allocation(conn, term, seed=None, actor="admin"):
    """Run the ballot for every open formal in `term`. Existing active
    allocations keep their seats; the ballot fills remaining capacity for
    users without a seat at that formal.
    Returns (run_id, seed, places, log, new_allocs)."""
    seed = seed or secrets.token_hex(8)
    with immediate(conn):
        capacity, unit_prefs, unit_sizes, unit_caps, unit_members = \
            build_ballot_inputs(conn, term)
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


# ------------------------------------------------------------------ GDPR: export / erase

def export_user_data(conn, user_id):
    """Everything we hold about a user, as a plain dict (right of access /
    portability). Joins in readable formal names."""
    u = conn.execute("SELECT id, first_name, last_name, email, email_verified, "
                     "dietary_flags, dietary_other, created_at FROM users WHERE id=?",
                     (user_id,)).fetchone()
    if u is None:
        return None
    d = dict(u)

    def rows(sql, args=(user_id,)):
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    d["preferences"] = rows(
        "SELECT p.term, p.rank, f.host_college, f.dt FROM preferences p "
        "JOIN formals f ON f.id=p.formal_id WHERE p.user_id=? ORDER BY p.term, p.rank")
    d["max_places_caps"] = rows("SELECT term, max_places FROM ballot_caps WHERE user_id=?")
    d["allocations"] = rows(
        "SELECT a.status, a.source, a.created_at, f.host_college, f.dt "
        "FROM allocations a JOIN formals f ON f.id=a.formal_id WHERE a.user_id=? "
        "ORDER BY f.dt")
    d["slot_alerts"] = rows(
        "SELECT f.host_college, f.dt FROM subscriptions s "
        "JOIN formals f ON f.id=s.formal_id WHERE s.user_id=?")
    d["group_memberships"] = rows(
        "SELECT g.term, g.party_name, m.status, "
        "(g.leader_user_id=?) AS is_leader FROM ballot_group_members m "
        "JOIN ballot_groups g ON g.id=m.group_id WHERE m.user_id=?", (user_id, user_id))
    d["reviews"] = rows(
        "SELECT f.host_college, f.dt, r.course_stars, r.vibe_stars, r.review, "
        "r.created_at FROM reviews r JOIN formals f ON f.id=r.formal_id "
        "WHERE r.user_id=? ORDER BY r.created_at")
    return d


def delete_user_data(conn, user_id):
    """Erase all of a user's personal data (right to erasure). Returns the list
    of photo filenames the caller should unlink from disk. A group the user
    leads is disbanded. Audit-log rows (numeric id only, no name/email) are
    retained for security/integrity under legitimate interest."""
    u = conn.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
    if u is None:
        return []
    photos = [r["filename"] for r in conn.execute(
        "SELECT rp.filename FROM review_photos rp JOIN reviews r ON r.id=rp.review_id "
        "WHERE r.user_id=?", (user_id,)).fetchall()]
    photos += [r["photo"] for r in conn.execute(
        "SELECT photo FROM reviews WHERE user_id=? AND photo != ''",
        (user_id,)).fetchall()]  # legacy single-photo column
    with immediate(conn):
        # Break released_slots references before deleting allocations.
        conn.execute("UPDATE released_slots SET claimed_by=NULL WHERE claimed_by=?",
                     (user_id,))
        conn.execute("UPDATE released_slots SET allocation_id=NULL WHERE allocation_id "
                     "IN (SELECT id FROM allocations WHERE user_id=?)", (user_id,))
        conn.execute("DELETE FROM review_photos WHERE review_id IN "
                     "(SELECT id FROM reviews WHERE user_id=?)", (user_id,))
        for t in ("reviews", "preferences", "ballot_caps", "subscriptions",
                  "allocations", "ballot_group_members"):
            conn.execute(f"DELETE FROM {t} WHERE user_id=?", (user_id,))  # noqa: S608 fixed names
        # Disband any group this user leads (remove all its members, then the group).
        led = [r["id"] for r in conn.execute(
            "SELECT id FROM ballot_groups WHERE leader_user_id=?", (user_id,)).fetchall()]
        for gid in led:
            conn.execute("DELETE FROM ballot_group_members WHERE group_id=?", (gid,))
            conn.execute("DELETE FROM ballot_groups WHERE id=?", (gid,))
        conn.execute("DELETE FROM login_attempts WHERE identifier=?", (u["email"],))
        audit(conn, f"user:{user_id}", "account_deleted", "GDPR erasure")
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return photos


# ------------------------------------------------------------------ peer-to-peer swaps

class SwapError(Exception):
    pass


def _holds_active(conn, user_id, formal_id):
    return conn.execute(
        "SELECT 1 FROM allocations WHERE user_id=? AND formal_id=? AND status='active'",
        (user_id, formal_id)).fetchone() is not None


def _swap_preconditions(conn, from_user, from_formal, to_user, to_formal):
    """Validate a proposed trade (A's place at from_formal <-> B's place at
    to_formal). Raises SwapError with a user-facing message."""
    if from_user == to_user:
        raise SwapError("You can't swap with yourself.")
    if from_formal == to_formal:
        raise SwapError("Pick two different formals.")
    if not _holds_active(conn, from_user, from_formal):
        raise SwapError("You no longer hold that place.")
    if not _holds_active(conn, to_user, to_formal):
        raise SwapError("The other person no longer holds that place.")
    if _holds_active(conn, from_user, to_formal):
        raise SwapError("You already have a place at that formal.")
    if _holds_active(conn, to_user, from_formal):
        raise SwapError("The other person already has a place at your formal.")
    cutoff = swap_cutoff_hours(conn)
    for fid in (from_formal, to_formal):
        dt = conn.execute("SELECT dt FROM formals WHERE id=?", (fid,)).fetchone()
        if dt is None:
            raise SwapError("That formal no longer exists.")
        if hours_until_formal(dt["dt"]) < cutoff:
            days = int(cutoff // 24)
            when = f"{days} day{'s' if days != 1 else ''}" if cutoff % 24 == 0 \
                else f"{int(cutoff)} hours"
            raise SwapError(f"Swaps must be arranged at least {when} before "
                            "both formals.")


def propose_swap(conn, from_user, from_formal, to_user, to_formal):
    _swap_preconditions(conn, from_user, from_formal, to_user, to_formal)
    dup = conn.execute(
        "SELECT 1 FROM swap_requests WHERE status='pending' AND from_user=? "
        "AND from_formal=? AND to_user=? AND to_formal=?",
        (from_user, from_formal, to_user, to_formal)).fetchone()
    if dup:
        raise SwapError("You've already sent this swap request.")
    cur = conn.execute(
        "INSERT INTO swap_requests(from_user, from_formal, to_user, to_formal) "
        "VALUES (?,?,?,?)", (from_user, from_formal, to_user, to_formal))
    audit(conn, f"user:{from_user}", "swap_propose",
          f"offer formal {from_formal} for {to_formal} to user {to_user}")
    return cur.lastrowid


def accept_swap(conn, req_id, accepting_user):
    """Atomically execute the trade. Returns the request row (for emailing)."""
    with immediate(conn):
        r = conn.execute("SELECT * FROM swap_requests WHERE id=? AND status='pending'",
                         (req_id,)).fetchone()
        if r is None or r["to_user"] != accepting_user:
            raise SwapError("This swap request is no longer available.")
        _swap_preconditions(conn, r["from_user"], r["from_formal"],
                            r["to_user"], r["to_formal"])
        # Move each place to the other person.
        conn.execute("UPDATE allocations SET user_id=?, source='swap' "
                     "WHERE user_id=? AND formal_id=? AND status='active'",
                     (r["to_user"], r["from_user"], r["from_formal"]))
        conn.execute("UPDATE allocations SET user_id=?, source='swap' "
                     "WHERE user_id=? AND formal_id=? AND status='active'",
                     (r["from_user"], r["to_user"], r["to_formal"]))
        conn.execute("UPDATE swap_requests SET status='accepted', "
                     "responded_at=? WHERE id=?", (utcnow_str(), req_id))
        # Any other pending requests that touch these now-moved places are stale.
        for uid, fid in ((r["from_user"], r["from_formal"]),
                         (r["to_user"], r["to_formal"])):
            conn.execute(
                "UPDATE swap_requests SET status='cancelled', responded_at=? "
                "WHERE status='pending' AND id!=? AND "
                "((from_user=? AND from_formal=?) OR (to_user=? AND to_formal=?))",
                (utcnow_str(), req_id, uid, fid, uid, fid))
        audit(conn, f"user:{accepting_user}", "swap_accept",
              f"req {req_id}: formal {r['from_formal']} <-> {r['to_formal']}")
        return r


def respond_swap(conn, req_id, user_id, action):
    """Decline (as recipient) or cancel (as proposer) a pending request."""
    r = conn.execute("SELECT * FROM swap_requests WHERE id=? AND status='pending'",
                     (req_id,)).fetchone()
    if r is None:
        raise SwapError("This swap request is no longer available.")
    if action == "decline" and r["to_user"] == user_id:
        status = "declined"
    elif action == "cancel" and r["from_user"] == user_id:
        status = "cancelled"
    else:
        raise SwapError("You can't do that to this request.")
    conn.execute("UPDATE swap_requests SET status=?, responded_at=? WHERE id=?",
                 (status, utcnow_str(), req_id))


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


def catering_list(conn, formal_id):
    """Active attendees of a formal with dietary info (for the host college)."""
    return conn.execute(
        "SELECT u.first_name, u.last_name, u.dietary_flags, u.dietary_other "
        "FROM allocations a JOIN users u ON u.id = a.user_id "
        "WHERE a.formal_id=? AND a.status='active' "
        "ORDER BY u.last_name, u.first_name", (formal_id,)).fetchall()


CATERING_LEAD_DAYS = 7


def send_scheduled_emails(conn, send_reminder, send_review, send_catering=None,
                          now=None):
    """Time-based emails. Called periodically by the worker thread.

    - One week before a formal (once results are published and it has
      attendees): the attendance list + dietary go to the host college, cc the
      admin_email setting.
    - 09:00 UK on the day of a formal: courtesy reminder to every attendee.
    - 19:30 UK on the day of the formal: review request to every attendee.

    Callbacks: send_reminder(f, [emails]), send_review(f, [emails]),
    send_catering(f, to, cc, rows). Flags are flipped inside an immediate
    transaction so each email goes at most once."""
    now = now or local_now()
    fired = []
    published = get_setting(conn, "results_published", "1") == "1"
    admin_email = get_setting(conn, "admin_email", "").strip()
    admin_name = get_setting(conn, "admin_name", "").strip()
    rows = conn.execute(
        "SELECT id FROM formals WHERE status IN ('open','allocated') "
        "AND (reminder_sent=0 OR review_sent=0 OR catering_sent=0)").fetchall()
    for row in rows:
        f = conn.execute("SELECT * FROM formals WHERE id=?", (row["id"],)).fetchone()
        start = parse_local(f["dt"])

        # Host catering list: once, from a week before up to the formal's start.
        if (send_catering and not f["catering_sent"]
                and start - timedelta(days=CATERING_LEAD_DAYS) <= now < start):
            host_email = (f["host_email"] or "").strip()
            recipient = host_email or admin_email
            attendees = catering_list(conn, f["id"])
            if published and attendees and recipient:
                with immediate(conn):
                    fresh = conn.execute("SELECT catering_sent FROM formals WHERE id=?",
                                         (f["id"],)).fetchone()
                    do_send = not fresh["catering_sent"]
                    if do_send:
                        conn.execute("UPDATE formals SET catering_sent=1 WHERE id=?",
                                     (f["id"],))
                if do_send:
                    cc = (admin_email if host_email and admin_email
                          and admin_email.lower() != host_email.lower() else None)
                    send_catering(f, recipient, cc, attendees, admin_name)
                    fired.append(("catering", f["id"]))

        if start.date() != now.date():
            # Reminder/review are strictly day-of; mark long-past formals done
            # so we stop scanning them.
            if start < now - timedelta(days=1):
                conn.execute("UPDATE formals SET reminder_sent=1, review_sent=1, "
                             "catering_sent=1 WHERE id=?", (f["id"],))
            continue
        nine = start.replace(hour=9, minute=0, second=0, microsecond=0)
        review_at = start.replace(hour=19, minute=30, second=0, microsecond=0)
        if not f["reminder_sent"] and nine <= now < start:
            with immediate(conn):
                fresh = conn.execute("SELECT reminder_sent FROM formals WHERE id=?",
                                     (f["id"],)).fetchone()
                if fresh["reminder_sent"]:
                    continue
                conn.execute("UPDATE formals SET reminder_sent=1 WHERE id=?", (f["id"],))
            send_reminder(f, attendee_emails(conn, f["id"]))
            fired.append(("reminder", f["id"]))
        if not f["review_sent"] and now >= review_at:
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
