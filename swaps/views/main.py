from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)

from ..db import get_db, get_setting, audit
from ..security import current_user, login_required
from ..services import (CancelError, ClaimError, cancel_allocation, claim_seat,
                        free_seats, hours_until_formal, local_now, parse_local)

bp = Blueprint("main", __name__)


def _term_formals(db, term):
    return db.execute(
        "SELECT * FROM formals WHERE term=? AND status IN ('open','allocated') "
        "ORDER BY dt", (term,)).fetchall()


def _ballot_window(db, term):
    """Ballot is open if now is inside ANY open formal's window (they are set
    per formal; admin normally sets the same window for the whole term)."""
    now = local_now()
    for f in db.execute("SELECT ballot_open, ballot_close FROM formals "
                        "WHERE term=? AND status='open'", (term,)).fetchall():
        if parse_local(f["ballot_open"]) <= now <= parse_local(f["ballot_close"]):
            return True
    return False


@bp.route("/")
def index():
    db = get_db()
    term = get_setting(db, "current_term")
    formals = _term_formals(db, term) if term else []
    return render_template("index.html", formals=formals, term=term,
                           ballot_open=_ballot_window(db, term) if term else False,
                           free_seats={f["id"]: free_seats(db, f["id"], f["slots"])
                                       for f in formals})


@bp.route("/rank", methods=["GET", "POST"])
@login_required
def rank():
    db = get_db()
    user = current_user()
    term = get_setting(db, "current_term")
    if not term:
        flash("No term is currently set up.", "error")
        return redirect(url_for("main.index"))
    open_formals = db.execute(
        "SELECT * FROM formals WHERE term=? AND status='open' ORDER BY dt",
        (term,)).fetchall()
    if not _ballot_window(db, term):
        flash("The ballot is not open right now.", "error")
        return redirect(url_for("main.index"))

    if request.method == "POST":
        order = request.form.get("order", "")
        ids = []
        for tok in order.split(","):
            tok = tok.strip()
            if tok.isdigit() and int(tok) not in ids:
                ids.append(int(tok))
        valid = {f["id"] for f in open_formals}
        ids = [i for i in ids if i in valid]
        db.execute("DELETE FROM preferences WHERE user_id=? AND term=?",
                   (user["id"], term))
        for rank_no, fid in enumerate(ids, start=1):
            db.execute("INSERT INTO preferences(user_id, formal_id, rank, term) "
                       "VALUES (?,?,?,?)", (user["id"], fid, rank_no, term))
        flash(f"Saved — you ranked {len(ids)} formal(s). You can edit until the "
              "ballot closes.", "ok")
        return redirect(url_for("main.rank"))

    prefs = db.execute(
        "SELECT formal_id FROM preferences WHERE user_id=? AND term=? ORDER BY rank",
        (user["id"], term)).fetchall()
    ranked_ids = [p["formal_id"] for p in prefs]
    by_id = {f["id"]: f for f in open_formals}
    ranked = [by_id[i] for i in ranked_ids if i in by_id]
    unranked = [f for f in open_formals if f["id"] not in ranked_ids]
    return render_template("rank.html", ranked=ranked, unranked=unranked, term=term)


@bp.route("/attendees")
def attendees():
    db = get_db()
    if get_setting(db, "attendee_list_public") != "1" and current_user() is None:
        return redirect(url_for("auth.login", next="/attendees"))
    term = get_setting(db, "current_term")
    formals = db.execute(
        "SELECT * FROM formals WHERE term=? AND status='allocated' ORDER BY dt",
        (term,)).fetchall()
    data = []
    for f in formals:
        rows = db.execute(
            "SELECT u.first_name, u.last_name, u.dietary_flags, u.dietary_other "
            "FROM allocations a JOIN users u ON u.id = a.user_id "
            "WHERE a.formal_id=? AND a.status='active' "
            "ORDER BY u.last_name, u.first_name", (f["id"],)).fetchall()
        diets = {}
        for r in rows:
            for d in filter(None, r["dietary_flags"].split(",")):
                diets[d] = diets.get(d, 0) + 1
            if r["dietary_other"]:
                diets[r["dietary_other"]] = diets.get(r["dietary_other"], 0) + 1
        data.append((f, rows, diets))
    return render_template("attendees.html", data=data, term=term)


@bp.route("/me")
@login_required
def me():
    db = get_db()
    user = current_user()
    allocs = db.execute(
        "SELECT a.id, a.status, f.host_college, f.dt, f.price, f.id AS formal_id "
        "FROM allocations a JOIN formals f ON f.id = a.formal_id "
        "WHERE a.user_id=? ORDER BY f.dt", (user["id"],)).fetchall()
    subs = db.execute(
        "SELECT s.id, f.host_college, f.dt, f.id AS formal_id FROM subscriptions s "
        "JOIN formals f ON f.id = s.formal_id WHERE s.user_id=? ORDER BY f.dt",
        (user["id"],)).fetchall()
    cancellable = {a["id"]: (a["status"] == "active"
                             and hours_until_formal(a["dt"]) >= 24) for a in allocs}
    return render_template("me.html", allocs=allocs, subs=subs, cancellable=cancellable)


@bp.route("/cancel/<int:alloc_id>", methods=["POST"])
@login_required
def cancel(alloc_id):
    db = get_db()
    try:
        cancel_allocation(db, current_user()["id"], alloc_id)
        flash("Cancelled. The place will be released to others at a random time "
              "within the next hour.", "ok")
    except CancelError as e:
        flash(str(e), "error")
    return redirect(url_for("main.me"))


@bp.route("/formals/<int:formal_id>/claim", methods=["GET", "POST"])
@login_required
def claim(formal_id):
    db = get_db()
    f = db.execute("SELECT * FROM formals WHERE id=?", (formal_id,)).fetchone()
    if f is None:
        abort(404)
    if request.method == "POST":
        try:
            claim_seat(db, current_user()["id"], formal_id)
            flash(f"You're in! You have a place at the {f['host_college']} formal.", "ok")
            return redirect(url_for("main.me"))
        except ClaimError as e:
            flash(str(e), "error")
    return render_template("claim.html", formal=f,
                           free=free_seats(db, formal_id, f["slots"]))


@bp.route("/formals/<int:formal_id>/subscribe", methods=["POST"])
@login_required
def subscribe(formal_id):
    db = get_db()
    if db.execute("SELECT 1 FROM formals WHERE id=?", (formal_id,)).fetchone() is None:
        abort(404)
    db.execute("INSERT OR IGNORE INTO subscriptions(user_id, formal_id) VALUES (?,?)",
               (current_user()["id"], formal_id))
    flash("Subscribed — we'll email you if a place opens up.", "ok")
    return redirect(request.referrer or url_for("main.index"))


@bp.route("/formals/<int:formal_id>/unsubscribe", methods=["POST"])
@login_required
def unsubscribe(formal_id):
    db = get_db()
    db.execute("DELETE FROM subscriptions WHERE user_id=? AND formal_id=?",
               (current_user()["id"], formal_id))
    flash("Unsubscribed.", "ok")
    return redirect(request.referrer or url_for("main.me"))
