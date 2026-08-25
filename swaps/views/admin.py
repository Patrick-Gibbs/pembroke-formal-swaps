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
    return render_template("admin/dashboard.html", formals=formals, stats=stats,
                           users_n=users_n,
                           term=get_setting(db, "current_term"),
                           list_public=get_setting(db, "attendee_list_public") == "1")


@bp.route("/settings", methods=["POST"])
@admin_required
def settings():
    db = get_db()
    term = request.form.get("current_term", "").strip()
    set_setting(db, "current_term", term)
    set_setting(db, "attendee_list_public",
                "1" if request.form.get("attendee_list_public") else "0")
    audit(db, "admin", "settings", f"term={term} "
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


def _send_result_emails(db, term, new_allocs):
    """Email winners their formals (+ calendar invites) and entrants who ended
    up with nothing a courteous miss note. Delivery happens on a background
    thread so the admin request returns immediately."""
    import threading

    from .. import emailer
    from ..ics import formal_ics

    by_user = {}
    for uid, fid in new_allocs:
        by_user.setdefault(uid, []).append(fid)
    winners = []
    for uid, fids in by_user.items():
        u = db.execute("SELECT email FROM users WHERE id=? AND email_verified=1",
                       (uid,)).fetchone()
        if not u:
            continue
        formals = [dict(db.execute("SELECT * FROM formals WHERE id=?", (fid,)).fetchone())
                   for fid in sorted(set(fids))]
        ics = [(f"{f['host_college']}-formal.ics", formal_ics(f).encode())
               for f in formals]
        winners.append((u["email"], formals, ics))

    # Entered (own prefs, or accepted member of a group whose leader entered)
    # but hold no active place this term at all -> miss email.
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
    missed = sorted(entrants - seated)

    def deliver():
        for email, formals, ics in winners:
            emailer.allocation_result_email(email, formals, ics)
        for email in missed:
            emailer.no_place_email(email, term)

    threading.Thread(target=deliver, daemon=True).start()
    return len(winners) + len(missed)


@bp.route("/allocate", methods=["GET", "POST"])
@admin_required
def allocate():
    db = get_db()
    term = get_setting(db, "current_term")
    if request.method == "POST":
        term = request.form.get("term", term).strip()
        seed = request.form.get("seed", "").strip() or None
        run_id, used_seed, placed, log, new_allocs = run_allocation(db, term, seed)
        emailed = _send_result_emails(db, term, new_allocs)
        flash(f"Allocation run #{run_id} complete: {placed} places assigned. "
              f"Seed: {used_seed}. Result emails queued to {emailed} member(s).",
              "ok")
        return redirect(url_for("admin.runs"))
    n_prefs = db.execute("SELECT COUNT(DISTINCT user_id) n FROM preferences WHERE term=?",
                         (term,)).fetchone()["n"]
    return render_template("admin/allocate.html", term=term, n_prefs=n_prefs)


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
