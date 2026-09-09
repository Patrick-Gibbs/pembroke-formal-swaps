import csv
import io

from flask import (Blueprint, Response, flash, redirect, render_template,
                   request, session, url_for)

from .. import config
from ..db import get_db, get_setting, set_setting, audit
from ..security import (admin_required, client_ip, ip_blocked,
                        lockout_remaining, record_attempt, verify_secret)
from ..services import (CancelError, ClaimError, cancel_allocation, claim_seat,
                        free_seats, run_allocation)

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        db = get_db()
        ip = client_ip()
        if ip_blocked(db, "admin", ip) or lockout_remaining(db, "admin", "admin"):
            flash("Too many failed attempts. Try again later.", "error")
            return render_template("admin/login.html"), 429
        password = request.form.get("password", "")
        ok = bool(config.ADMIN_PASSWORD_HASH) and verify_secret(
            config.ADMIN_PASSWORD_HASH, password)
        record_attempt(db, "admin", "admin", ip, ok)
        if not ok:
            flash("Wrong password.", "error")
            return render_template("admin/login.html"), 401
        session.clear()
        session["is_admin"] = True
        audit(db, "admin", "admin_login", f"ip={ip}")
        return redirect(url_for("admin.dashboard"))
    return render_template("admin/login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("main.index"))


@bp.route("/")
@admin_required
def dashboard():
    db = get_db()
    formals = db.execute("SELECT * FROM formals ORDER BY term, dt").fetchall()
    stats = {}
    for f in formals:
        active = db.execute("SELECT COUNT(*) n FROM allocations WHERE formal_id=? "
                            "AND status='active'", (f["id"],)).fetchone()["n"]
        prefs = db.execute("SELECT COUNT(*) n FROM preferences WHERE formal_id=?",
                           (f["id"],)).fetchone()["n"]
        subs = db.execute("SELECT COUNT(*) n FROM subscriptions WHERE formal_id=?",
                          (f["id"],)).fetchone()["n"]
        stats[f["id"]] = {"active": active, "prefs": prefs, "subs": subs,
                          "free": free_seats(db, f["id"], f["slots"])}
    users_n = db.execute("SELECT COUNT(*) n FROM users WHERE email_verified=1"
                         ).fetchone()["n"]
    term_now = get_setting(db, "current_term")
    rosters = {}
    for f in formals:
        if f["term"] == term_now:
            rosters[f["id"]] = db.execute(
                "SELECT u.first_name, u.last_name, u.email, u.dietary_flags, "
                "u.dietary_other FROM allocations a JOIN users u ON u.id=a.user_id "
                "WHERE a.formal_id=? AND a.status='active' "
                "ORDER BY u.last_name, u.first_name", (f["id"],)).fetchall()
    unnotified = db.execute(
        "SELECT COUNT(*) n FROM allocations a JOIN formals f ON f.id=a.formal_id "
        "WHERE a.status='active' AND a.notified=0 AND f.term=?",
        (term_now,)).fetchone()["n"]
    return render_template("admin/dashboard.html", formals=formals, stats=stats,
                           users_n=users_n, unnotified=unnotified, rosters=rosters,
                           results_published=get_setting(db, "results_published",
                                                         "1") == "1",
                           term=get_setting(db, "current_term"),
                           term_ballot_open=get_setting(db, "term_ballot_open"),
                           term_ballot_close=get_setting(db, "term_ballot_close"),
                           cancel_cutoff=get_setting(db, "cancel_cutoff_hours", "72"),
                           list_public=get_setting(db, "attendee_list_public") == "1")


@bp.route("/settings", methods=["POST"])
@admin_required
def settings():
    db = get_db()
    term = request.form.get("current_term", "").strip()
    if term != get_setting(db, "current_term"):
        set_setting(db, "results_published", "0")  # new term starts unpublished
    set_setting(db, "current_term", term)
    set_setting(db, "attendee_list_public",
                "1" if request.form.get("attendee_list_public") else "0")
    t_open = request.form.get("term_ballot_open", "").replace("T", " ").strip()
    t_close = request.form.get("term_ballot_close", "").replace("T", " ").strip()
    set_setting(db, "term_ballot_open", t_open)
    set_setting(db, "term_ballot_close", t_close)
    try:
        cutoff = max(0, int(request.form.get("cancel_cutoff_hours", "72")))
    except ValueError:
        cutoff = 72
    set_setting(db, "cancel_cutoff_hours", str(cutoff))
    audit(db, "admin", "settings", f"term={term} window={t_open}..{t_close} "
          f"public={request.form.get('attendee_list_public', '0')}")
    flash("Settings saved.", "ok")
    return redirect(url_for("admin.dashboard"))


def _formal_from_form():
    return (request.form.get("host_college", "").strip(),
            request.form.get("dt", "").replace("T", " ").strip(),
            request.form.get("price", "").strip(),
            int(request.form.get("slots", "0") or 0),
            request.form.get("term", "").strip(),
            request.form.get("ballot_open", "").replace("T", " ").strip(),
            request.form.get("ballot_close", "").replace("T", " ").strip(),
            request.form.get("status", "open"),
            request.form.get("location", "").strip()[:300],
            request.form.get("instructions", "").strip()[:2000])


@bp.route("/formals/new", methods=["GET", "POST"])
@admin_required
def formal_new():
    db = get_db()
    if request.method == "POST":
        vals = _formal_from_form()
        if not vals[0] or not vals[1] or vals[3] < 1:
            flash("College, date/time and a positive slot count are required.", "error")
        else:
            db.execute("INSERT INTO formals(host_college, dt, price, slots, term, "
                       "ballot_open, ballot_close, status, location, instructions) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)", vals)
            audit(db, "admin", "formal_create", f"{vals[0]} {vals[1]}")
            flash("Formal created.", "ok")
            return redirect(url_for("admin.dashboard"))
    return render_template("admin/formal_form.html", f=None,
                           default_term=get_setting(db, "current_term"))


@bp.route("/formals/<int:fid>/edit", methods=["GET", "POST"])
@admin_required
def formal_edit(fid):
    db = get_db()
    f = db.execute("SELECT * FROM formals WHERE id=?", (fid,)).fetchone()
    if f is None:
        flash("No such formal.", "error")
        return redirect(url_for("admin.dashboard"))
    if request.method == "POST":
        vals = _formal_from_form()
        db.execute("UPDATE formals SET host_college=?, dt=?, price=?, slots=?, term=?, "
                   "ballot_open=?, ballot_close=?, status=?, location=?, "
                   "instructions=? WHERE id=?", vals + (fid,))
        audit(db, "admin", "formal_edit", f"id={fid} {vals[0]} {vals[1]}")
        flash("Saved.", "ok")
        return redirect(url_for("admin.dashboard"))
    return render_template("admin/formal_form.html", f=f, default_term=f["term"])


@bp.route("/formals/<int:fid>/delete", methods=["POST"])
@admin_required
def formal_delete(fid):
    db = get_db()
    n = db.execute("SELECT COUNT(*) n FROM allocations WHERE formal_id=? AND "
                   "status='active'", (fid,)).fetchone()["n"]
    if n:
        flash(f"Refusing to delete: {n} active allocation(s). Cancel them first "
              "or set status to cancelled.", "error")
        return redirect(url_for("admin.dashboard"))
    for table in ("preferences", "subscriptions", "released_slots"):
        db.execute(f"DELETE FROM {table} WHERE formal_id=?", (fid,))  # noqa: S608 - fixed table names
    db.execute("DELETE FROM allocations WHERE formal_id=?", (fid,))
    db.execute("DELETE FROM formals WHERE id=?", (fid,))
    audit(db, "admin", "formal_delete", f"id={fid}")
    flash("Deleted.", "ok")
    return redirect(url_for("admin.dashboard"))


@bp.route("/allocate", methods=["GET", "POST"])
@admin_required
def allocate():
    db = get_db()
    term = get_setting(db, "current_term")
    if request.method == "POST":
        term = request.form.get("term", term).strip()
        seed = request.form.get("seed", "").strip() or None
        run_id, used_seed, placed, log, new_allocs = run_allocation(db, term, seed)
        # First generate of the term? Hold results as a draft until Publish.
        already_public = db.execute(
            "SELECT 1 FROM allocations a JOIN formals f ON f.id=a.formal_id "
            "WHERE a.status='active' AND a.notified=1 AND f.term=? LIMIT 1",
            (term,)).fetchone()
        if not already_public:
            set_setting(db, "results_published", "0")
        flash(f"Generated: run #{run_id}, {placed} places assigned "
              f"(seed {used_seed}). This is a DRAFT — no emails sent yet. "
              f"Check the rosters, then hit Publish on the dashboard.", "ok")
        return redirect(url_for("admin.dashboard"))
    n_prefs = db.execute("SELECT COUNT(DISTINCT user_id) n FROM preferences WHERE term=?",
                         (term,)).fetchone()["n"]
    return render_template("admin/allocate.html", term=term, n_prefs=n_prefs)


@bp.route("/preview")
@admin_required
def preview():
    """One-page sanity check before publishing: every formal for the current
    term (date order) with its attendees, plus entrants left with nothing."""
    db = get_db()
    term = get_setting(db, "current_term")
    formals = db.execute(
        "SELECT * FROM formals WHERE term=? AND status IN ('open','allocated') "
        "ORDER BY dt", (term,)).fetchall()
    rosters = {}
    for f in formals:
        rosters[f["id"]] = db.execute(
            "SELECT u.first_name, u.last_name, u.dietary_flags, u.dietary_other, "
            "a.source, a.notified FROM allocations a JOIN users u ON u.id=a.user_id "
            "WHERE a.formal_id=? AND a.status='active' "
            "ORDER BY u.last_name, u.first_name", (f["id"],)).fetchall()
    entrants = {r["id"]: r for r in db.execute(
        "SELECT DISTINCT u.* FROM users u JOIN preferences p ON p.user_id=u.id "
        "WHERE p.term=? AND u.email_verified=1", (term,)).fetchall()}
    for r in db.execute(
            "SELECT DISTINCT u.* FROM users u "
            "JOIN ballot_group_members m ON m.user_id=u.id AND m.status='accepted' "
            "JOIN ballot_groups g ON g.id=m.group_id "
            "JOIN preferences p ON p.user_id=g.leader_user_id AND p.term=g.term "
            "WHERE g.term=? AND u.email_verified=1", (term,)).fetchall():
        entrants[r["id"]] = r
    seated_ids = {r["user_id"] for r in db.execute(
        "SELECT DISTINCT a.user_id FROM allocations a "
        "JOIN formals f ON f.id=a.formal_id "
        "WHERE a.status='active' AND f.term=?", (term,)).fetchall()}
    unseated = sorted((u for uid, u in entrants.items() if uid not in seated_ids),
                      key=lambda u: (u["last_name"], u["first_name"]))
    return render_template(
        "admin/preview.html", term=term, formals=formals, rosters=rosters,
        unseated=unseated,
        results_published=get_setting(db, "results_published", "1") == "1")


def _result_batches(db, term, only_unnotified):
    """(winners, missed, alloc_ids): winners = [(email, formals, ics_files)]
    grouped per user; missed = entrant emails with no active place this term."""
    from ..ics import formal_ics

    q = ("SELECT a.id, a.user_id, a.formal_id FROM allocations a "
         "JOIN formals f ON f.id=a.formal_id "
         "WHERE a.status='active' AND f.term=?")
    if only_unnotified:
        q += " AND a.notified=0"
    rows = db.execute(q, (term,)).fetchall()
    by_user = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append(r["formal_id"])
    winners = []
    for uid, fids in by_user.items():
        u = db.execute("SELECT email FROM users WHERE id=? AND email_verified=1",
                       (uid,)).fetchone()
        if not u:
            continue
        formals = [dict(db.execute("SELECT * FROM formals WHERE id=?",
                                   (fid,)).fetchone())
                   for fid in sorted(set(fids))]
        ics = [(f"{f['host_college']}-formal.ics", formal_ics(f).encode())
               for f in formals]
        winners.append((u["email"], formals, ics))

    # Entered (own prefs, or accepted member of a group whose leader entered)
    # but hold no active place this term -> miss email.
    entrants = {r["email"] for r in db.execute(
        "SELECT DISTINCT u.email FROM users u JOIN preferences p ON p.user_id=u.id "
        "WHERE p.term=? AND u.email_verified=1", (term,)).fetchall()}
    entrants |= {r["email"] for r in db.execute(
        "SELECT DISTINCT u.email FROM users u "
        "JOIN ballot_group_members m ON m.user_id=u.id AND m.status='accepted' "
        "JOIN ballot_groups g ON g.id=m.group_id "
        "JOIN preferences p ON p.user_id=g.leader_user_id AND p.term=g.term "
        "WHERE g.term=? AND u.email_verified=1", (term,)).fetchall()}
    seated = {r["email"] for r in db.execute(
        "SELECT DISTINCT u.email FROM users u JOIN allocations a ON a.user_id=u.id "
        "JOIN formals f ON f.id=a.formal_id "
        "WHERE a.status='active' AND f.term=?", (term,)).fetchall()}
    return winners, sorted(entrants - seated), [r["id"] for r in rows]


def _deliver_results(winners, missed, term):
    import threading

    from .. import emailer

    def deliver():
        for email, formals, ics in winners:
            emailer.allocation_result_email(email, formals, ics)
        for email in missed:
            emailer.no_place_email(email, term)

    threading.Thread(target=deliver, daemon=True).start()


@bp.route("/publish", methods=["POST"])
@admin_required
def publish():
    """Make results public and email every not-yet-notified winner their
    formals (+ calendar invites); entrants left with nothing get a miss note."""
    db = get_db()
    term = get_setting(db, "current_term")
    winners, missed, alloc_ids = _result_batches(db, term, only_unnotified=True)
    first_publish = get_setting(db, "results_published") != "1"
    if not first_publish:
        missed = []  # only nag the unlucky once, on first publish
    if alloc_ids:
        db.execute("UPDATE allocations SET notified=1 WHERE id IN (%s)"
                   % ",".join("?" * len(alloc_ids)), alloc_ids)
    set_setting(db, "results_published", "1")
    audit(db, "admin", "publish_results",
          f"term={term} winners={len(winners)} missed={len(missed)}")
    _deliver_results(winners, missed, term)
    flash(f"Published. Result emails queued: {len(winners)} winner(s)"
          + (f", {len(missed)} without a place" if missed else "")
          + ". The assigned swaps page is now public.", "ok")
    return redirect(url_for("admin.dashboard"))


@bp.route("/republish", methods=["POST"])
@admin_required
def republish():
    """Resend result emails to EVERYONE for the current term: every member
    with active places gets their full result again (+ calendar invites),
    every entrant without a place gets the miss note again."""
    db = get_db()
    term = get_setting(db, "current_term")
    winners, missed, alloc_ids = _result_batches(db, term, only_unnotified=False)
    if alloc_ids:
        db.execute("UPDATE allocations SET notified=1 WHERE id IN (%s)"
                   % ",".join("?" * len(alloc_ids)), alloc_ids)
    set_setting(db, "results_published", "1")
    audit(db, "admin", "republish_results",
          f"term={term} winners={len(winners)} missed={len(missed)}")
    _deliver_results(winners, missed, term)
    flash(f"Republished: result emails re-queued to {len(winners)} winner(s) "
          f"and {len(missed)} entrant(s) without a place.", "ok")
    return redirect(url_for("admin.dashboard"))


@bp.route("/runs")
@admin_required
def runs():
    db = get_db()
    rows = db.execute("SELECT * FROM allocation_runs ORDER BY id DESC").fetchall()
    return render_template("admin/runs.html", runs=rows)


@bp.route("/formals/<int:fid>/roster")
@admin_required
def roster(fid):
    db = get_db()
    f = db.execute("SELECT * FROM formals WHERE id=?", (fid,)).fetchone()
    if f is None:
        return redirect(url_for("admin.dashboard"))
    rows = db.execute(
        "SELECT a.id AS alloc_id, a.status, a.source, u.id AS user_id, u.first_name, "
        "u.last_name, u.email, u.dietary_flags, u.dietary_other "
        "FROM allocations a JOIN users u ON u.id = a.user_id "
        "WHERE a.formal_id=? ORDER BY a.status, u.last_name", (fid,)).fetchall()
    if request.args.get("csv"):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["first_name", "last_name", "email", "dietary", "status", "source"])
        for r in rows:
            if r["status"] != "active":
                continue
            diet = ", ".join(filter(None, [r["dietary_flags"].replace(",", "; "),
                                           r["dietary_other"]]))
            w.writerow([r["first_name"], r["last_name"], r["email"], diet,
                        r["status"], r["source"]])
        return Response(buf.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition":
                f"attachment; filename=formal-{fid}-{f['host_college']}.csv"})
    users = db.execute("SELECT id, first_name, last_name, email FROM users "
                       "WHERE email_verified=1 ORDER BY last_name").fetchall()
    return render_template("admin/roster.html", f=f, rows=rows, users=users,
                           free=free_seats(db, fid, f["slots"]))


@bp.route("/formals/<int:fid>/add-user", methods=["POST"])
@admin_required
def add_user(fid):
    db = get_db()
    uid = int(request.form.get("user_id", "0") or 0)
    try:
        claim_seat(db, uid, fid, actor="admin")
        audit(db, "admin", "manual_assign", f"user={uid} formal={fid}")
        flash("User added to formal.", "ok")
    except ClaimError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.roster", fid=fid))


@bp.route("/allocations/<int:alloc_id>/cancel", methods=["POST"])
@admin_required
def cancel_alloc(alloc_id):
    db = get_db()
    try:
        # user_id=None: admin override — bypasses ownership and the 24h rule
        cancel_allocation(db, None, alloc_id, actor="admin")
        flash("Allocation cancelled; slot will release within the hour.", "ok")
    except CancelError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("admin.dashboard"))


@bp.route("/formals/<int:fid>/email-all", methods=["POST"])
@admin_required
def email_all(fid):
    """Send a custom email to every active attendee of an outgoing formal."""
    import threading

    from .. import emailer
    from ..services import attendee_emails

    db = get_db()
    f = db.execute("SELECT * FROM formals WHERE id=?", (fid,)).fetchone()
    if f is None:
        return redirect(url_for("admin.dashboard"))
    subject = request.form.get("subject", "").strip()[:200]
    body = request.form.get("body", "").strip()[:8000]
    if not subject or not body:
        flash("Subject and message are both required.", "error")
        return redirect(url_for("admin.roster", fid=fid))
    html = "".join(f"<p>{line}</p>" for line in body.splitlines() if line.strip())
    recipients = attendee_emails(db, fid)
    audit(db, "admin", "email_all",
          f"formal={fid} subject={subject!r} recipients={len(recipients)}")

    def deliver():
        for email in recipients:
            emailer.send(email, subject, html)

    threading.Thread(target=deliver, daemon=True).start()
    flash(f"Email queued to {len(recipients)} attendee(s) of the "
          f"{f['host_college']} formal.", "ok")
    return redirect(url_for("admin.roster", fid=fid))


@bp.route("/export.csv")
@admin_required
def export_all():
    """Every formal's active attendees (all terms unless ?term= given)."""
    db = get_db()
    term = request.args.get("term", "")
    q = ("SELECT f.host_college, f.dt, f.price, f.term, u.first_name, u.last_name, "
         "u.email, u.dietary_flags, u.dietary_other FROM allocations a "
         "JOIN formals f ON f.id = a.formal_id JOIN users u ON u.id = a.user_id "
         "WHERE a.status='active'")
    args = []
    if term:
        q += " AND f.term=?"
        args.append(term)
    q += " ORDER BY f.dt, u.last_name, u.first_name"
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["formal", "date", "price", "term", "first_name", "last_name",
                "email", "dietary"])
    for r in db.execute(q, args).fetchall():
        diet = ", ".join(filter(None, [r["dietary_flags"].replace(",", "; "),
                                       r["dietary_other"]]))
        w.writerow([r["host_college"], r["dt"], r["price"], r["term"],
                    r["first_name"], r["last_name"], r["email"], diet])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=outgoing-swaps-all.csv"})


# ---------------------------------------------------------------- incoming swaps

@bp.route("/incoming")
@admin_required
def incoming():
    db = get_db()
    swaps = db.execute("SELECT * FROM incoming_swaps ORDER BY dt").fetchall()
    participants = {}
    for s in swaps:
        participants[s["id"]] = db.execute(
            "SELECT * FROM incoming_participants WHERE swap_id=? "
            "ORDER BY last_name, first_name", (s["id"],)).fetchall()
    return render_template("admin/incoming.html", swaps=swaps,
                           participants=participants)


@bp.route("/incoming/export.csv")
@admin_required
def incoming_export():
    db = get_db()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["guest_college", "date", "host_name", "host_email", "host_phone",
                "swap_notes", "first_name", "last_name", "dietary", "guest_notes"])
    for s in db.execute("SELECT * FROM incoming_swaps ORDER BY dt").fetchall():
        people = db.execute(
            "SELECT * FROM incoming_participants WHERE swap_id=? "
            "ORDER BY last_name, first_name", (s["id"],)).fetchall()
        if not people:
            w.writerow([s["guest_college"], s["dt"], s["host_name"],
                        s["host_email"], s["host_phone"], s["notes"],
                        "", "", "", ""])
        for p in people:
            w.writerow([s["guest_college"], s["dt"], s["host_name"],
                        s["host_email"], s["host_phone"], s["notes"],
                        p["first_name"], p["last_name"], p["dietary"], p["notes"]])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=incoming-swaps-all.csv"})


def _incoming_from_form():
    return (request.form.get("guest_college", "").strip()[:120],
            request.form.get("dt", "").replace("T", " ").strip(),
            request.form.get("host_name", "").strip()[:120],
            request.form.get("host_email", "").strip()[:200],
            request.form.get("host_phone", "").strip()[:50],
            request.form.get("notes", "").strip()[:2000])


@bp.route("/incoming/new", methods=["POST"])
@admin_required
def incoming_new():
    db = get_db()
    vals = _incoming_from_form()
    if not vals[0] or not vals[1]:
        flash("Guest college and date/time are required.", "error")
    else:
        db.execute("INSERT INTO incoming_swaps(guest_college, dt, host_name, "
                   "host_email, host_phone, notes) VALUES (?,?,?,?,?,?)", vals)
        audit(db, "admin", "incoming_create", f"{vals[0]} {vals[1]}")
        flash("Incoming swap added.", "ok")
    return redirect(url_for("admin.incoming"))


@bp.route("/incoming/<int:sid>/update", methods=["POST"])
@admin_required
def incoming_update(sid):
    db = get_db()
    vals = _incoming_from_form()
    db.execute("UPDATE incoming_swaps SET guest_college=?, dt=?, host_name=?, "
               "host_email=?, host_phone=?, notes=? WHERE id=?", vals + (sid,))
    audit(db, "admin", "incoming_update", f"id={sid} {vals[0]}")
    flash("Saved.", "ok")
    return redirect(url_for("admin.incoming"))


@bp.route("/incoming/<int:sid>/delete", methods=["POST"])
@admin_required
def incoming_delete(sid):
    db = get_db()
    db.execute("DELETE FROM incoming_participants WHERE swap_id=?", (sid,))
    db.execute("DELETE FROM incoming_swaps WHERE id=?", (sid,))
    audit(db, "admin", "incoming_delete", f"id={sid}")
    flash("Incoming swap deleted.", "ok")
    return redirect(url_for("admin.incoming"))


@bp.route("/incoming/<int:sid>/participants/add", methods=["POST"])
@admin_required
def incoming_participant_add(sid):
    db = get_db()
    first = request.form.get("first_name", "").strip()[:80]
    last = request.form.get("last_name", "").strip()[:80]
    if not first or not last:
        flash("First and last name required.", "error")
    else:
        db.execute("INSERT INTO incoming_participants(swap_id, first_name, "
                   "last_name, dietary, notes) VALUES (?,?,?,?,?)",
                   (sid, first, last,
                    request.form.get("dietary", "").strip()[:300],
                    request.form.get("notes", "").strip()[:300]))
        flash(f"Added {first} {last}.", "ok")
    return redirect(url_for("admin.incoming"))


@bp.route("/incoming/participants/<int:pid>/update", methods=["POST"])
@admin_required
def incoming_participant_update(pid):
    db = get_db()
    db.execute("UPDATE incoming_participants SET first_name=?, last_name=?, "
               "dietary=?, notes=? WHERE id=?",
               (request.form.get("first_name", "").strip()[:80],
                request.form.get("last_name", "").strip()[:80],
                request.form.get("dietary", "").strip()[:300],
                request.form.get("notes", "").strip()[:300], pid))
    flash("Saved.", "ok")
    return redirect(url_for("admin.incoming"))


@bp.route("/incoming/participants/<int:pid>/delete", methods=["POST"])
@admin_required
def incoming_participant_delete(pid):
    db = get_db()
    db.execute("DELETE FROM incoming_participants WHERE id=?", (pid,))
    flash("Removed.", "ok")
    return redirect(url_for("admin.incoming"))


@bp.route("/users")
@admin_required
def users():
    db = get_db()
    rows = db.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM allocations a WHERE a.user_id=u.id AND "
        "a.status='active') AS places FROM users u ORDER BY u.last_name").fetchall()
    return render_template("admin/users.html", rows=rows)


@bp.route("/subscriptions")
@admin_required
def subscriptions():
    db = get_db()
    rows = db.execute(
        "SELECT s.id, u.email, u.first_name, u.last_name, f.host_college, f.dt "
        "FROM subscriptions s JOIN users u ON u.id=s.user_id "
        "JOIN formals f ON f.id=s.formal_id ORDER BY f.dt").fetchall()
    releases = db.execute(
        "SELECT r.*, f.host_college FROM released_slots r "
        "JOIN formals f ON f.id=r.formal_id ORDER BY r.id DESC LIMIT 50").fetchall()
    return render_template("admin/subscriptions.html", rows=rows, releases=releases)


@bp.route("/audit")
@admin_required
def audit_view():
    db = get_db()
    rows = db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 500").fetchall()
    return render_template("admin/audit.html", rows=rows)
