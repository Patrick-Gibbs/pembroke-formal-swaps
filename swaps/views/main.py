import os
import secrets
import statistics

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   send_from_directory, url_for)

from .. import config
from ..db import get_db, get_setting, audit
from ..security import current_user, login_required
from ..services import (CancelError, ClaimError, cancel_allocation,
                        cancel_cutoff_hours, claim_seat, free_seats,
                        hours_until_formal, local_now, parse_local)

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

bp = Blueprint("main", __name__)


def _term_formals(db, term):
    return db.execute(
        "SELECT * FROM formals WHERE term=? AND status IN ('open','allocated') "
        "ORDER BY dt", (term,)).fetchall()


def _effective_window(db, f):
    """A formal's ballot window: its own override if set, else the term-wide
    window from settings. Returns (open_str, close_str), either may be ''."""
    return (f["ballot_open"] or get_setting(db, "term_ballot_open"),
            f["ballot_close"] or get_setting(db, "term_ballot_close"))


def _ballot_window(db, term):
    """True if any open formal's effective window contains now."""
    now = local_now()
    for f in db.execute("SELECT ballot_open, ballot_close FROM formals "
                        "WHERE term=? AND status='open'", (term,)).fetchall():
        o, c = _effective_window(db, f)
        if o and c and parse_local(o) <= now <= parse_local(c):
            return True
    return False


@bp.route("/")
def index():
    db = get_db()
    term = get_setting(db, "current_term")
    formals = _term_formals(db, term) if term else []
    t_open = get_setting(db, "term_ballot_open")
    t_close = get_setting(db, "term_ballot_close")
    opens_soon = bool(t_open) and local_now() < parse_local(t_open)
    return render_template("index.html", formals=formals, term=term,
                           ballot_open=_ballot_window(db, term) if term else False,
                           ballot_closes=t_close, ballot_opens=t_open,
                           opens_soon=opens_soon,
                           cutoff_h=int(cancel_cutoff_hours(db)),
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

    from .ballot import accepted_group
    group = accepted_group(db, user["id"], term)
    is_group_member = bool(group and group["leader_user_id"] != user["id"])
    group_size = 0
    if group:
        group_size = db.execute(
            "SELECT COUNT(*) n FROM ballot_group_members WHERE group_id=? "
            "AND status='accepted'", (group["id"],)).fetchone()["n"]

    if is_group_member:
        if request.method == "POST":
            flash("You're in a group — your leader sets the ranking.", "error")
        leader = db.execute("SELECT first_name, last_name FROM users WHERE id=?",
                            (group["leader_user_id"],)).fetchone()
        return render_template("rank.html", ranked=[], unranked=[], term=term,
                               is_group_member=True, leader=leader,
                               group_size=group_size)

    if request.method == "POST":
        order = request.form.get("order", "")
        ids = []
        for tok in order.split(","):
            tok = tok.strip()
            if tok.isdigit() and int(tok) not in ids:
                ids.append(int(tok))
        valid = {f["id"] for f in open_formals}
        ids = [i for i in ids if i in valid]
        try:
            max_places = min(3, max(1, int(request.form.get("max_places", "3"))))
        except ValueError:
            max_places = 3
        db.execute("DELETE FROM preferences WHERE user_id=? AND term=?",
                   (user["id"], term))
        for rank_no, fid in enumerate(ids, start=1):
            db.execute("INSERT INTO preferences(user_id, formal_id, rank, term) "
                       "VALUES (?,?,?,?)", (user["id"], fid, rank_no, term))
        db.execute("INSERT INTO ballot_caps(user_id, term, max_places) "
                   "VALUES (?,?,?) ON CONFLICT(user_id, term) "
                   "DO UPDATE SET max_places=excluded.max_places",
                   (user["id"], term, max_places))
        flash(f"Saved — you ranked {len(ids)} formal(s), happy with up to "
              f"{max_places}. You can edit until the ballot closes.", "ok")
        return redirect(url_for("main.rank"))

    prefs = db.execute(
        "SELECT formal_id FROM preferences WHERE user_id=? AND term=? ORDER BY rank",
        (user["id"], term)).fetchall()
    ranked_ids = [p["formal_id"] for p in prefs]
    by_id = {f["id"]: f for f in open_formals}
    ranked = [by_id[i] for i in ranked_ids if i in by_id]
    unranked = [f for f in open_formals if f["id"] not in ranked_ids]
    cap_row = db.execute("SELECT max_places FROM ballot_caps WHERE user_id=? "
                         "AND term=?", (user["id"], term)).fetchone()
    return render_template("rank.html", ranked=ranked, unranked=unranked, term=term,
                           is_group_member=False, leader=None, group_size=group_size,
                           max_places=cap_row["max_places"] if cap_row else 3,
                           ballot_closes=get_setting(db, "term_ballot_close"))


@bp.route("/attendees")
def attendees():
    from flask import session
    db = get_db()
    if get_setting(db, "attendee_list_public") != "1" and current_user() is None:
        return redirect(url_for("auth.login", next="/attendees"))
    if (get_setting(db, "results_published", "1") != "1"
            and not session.get("is_admin")):
        return render_template("attendees.html", data=[], unpublished=True,
                               term=get_setting(db, "current_term"))
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
    return render_template("attendees.html", data=data, term=term,
                           unpublished=False)


@bp.route("/incoming")
def incoming():
    db = get_db()
    swaps = db.execute("SELECT * FROM incoming_swaps ORDER BY dt").fetchall()
    participants = {}
    for s in swaps:
        participants[s["id"]] = db.execute(
            "SELECT first_name, last_name FROM incoming_participants "
            "WHERE swap_id=? ORDER BY last_name, first_name", (s["id"],)).fetchall()
    return render_template("incoming.html", swaps=swaps, participants=participants)


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
    cutoff = cancel_cutoff_hours(db)
    cancellable = {a["id"]: (a["status"] == "active"
                             and hours_until_formal(a["dt"]) >= cutoff)
                   for a in allocs}
    past = {a["id"]: (a["status"] == "active" and hours_until_formal(a["dt"]) < 0)
            for a in allocs}
    return render_template("me.html", allocs=allocs, subs=subs,
                           cancellable=cancellable, past=past,
                           cutoff_h=int(cutoff))


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


@bp.route("/review/<int:formal_id>", methods=["GET", "POST"])
@login_required
def review(formal_id):
    db = get_db()
    user = current_user()
    f = db.execute("SELECT * FROM formals WHERE id=?", (formal_id,)).fetchone()
    if f is None:
        abort(404)
    attended = db.execute(
        "SELECT 1 FROM allocations WHERE user_id=? AND formal_id=? AND status='active'",
        (user["id"], formal_id)).fetchone()
    if not attended:
        flash("Only attendees of a formal can review it.", "error")
        return redirect(url_for("main.reviews_page"))
    if parse_local(f["dt"]) > local_now():
        flash("You can review after the formal has happened.", "error")
        return redirect(url_for("main.me"))

    existing = db.execute("SELECT * FROM reviews WHERE user_id=? AND formal_id=?",
                          (user["id"], formal_id)).fetchone()
    if request.method == "POST":
        course_stars = sum(1 for c in ("course1", "course2", "course3")
                           if request.form.get(c))
        vibe_stars = sum(1 for v in ("vibe_hosts", "vibe_college")
                         if request.form.get(v))
        text = request.form.get("review", "").strip()[:2000]

        photo_name = existing["photo"] if existing else ""
        file = request.files.get("photo")
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in PHOTO_EXTS:
                flash("Photo must be a JPG, PNG, WebP or GIF.", "error")
                return render_template("review_form.html", f=f, r=existing)
            os.makedirs(config.PHOTOS_DIR, exist_ok=True)
            if photo_name:  # replacing an earlier upload
                try:
                    os.remove(os.path.join(config.PHOTOS_DIR, photo_name))
                except OSError:
                    pass
            photo_name = f"r{formal_id}-{user['id']}-{secrets.token_hex(4)}{ext}"
            file.save(os.path.join(config.PHOTOS_DIR, photo_name))

        db.execute(
            "INSERT INTO reviews(user_id, formal_id, course_stars, vibe_stars, "
            "review, photo) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user_id, formal_id) DO UPDATE SET course_stars=excluded."
            "course_stars, vibe_stars=excluded.vibe_stars, review=excluded.review, "
            "photo=excluded.photo, created_at=datetime('now')",
            (user["id"], formal_id, course_stars, vibe_stars, text, photo_name))
        flash(f"Thanks — you rated it {course_stars + vibe_stars}/5. "
              "You can edit your review any time.", "ok")
        return redirect(url_for("main.reviews_page"))
    return render_template("review_form.html", f=f, r=existing)


@bp.route("/reviews")
def reviews_page():
    db = get_db()
    rows = db.execute(
        "SELECT r.*, f.host_college, f.dt, u.first_name, u.last_name "
        "FROM reviews r JOIN formals f ON f.id = r.formal_id "
        "JOIN users u ON u.id = r.user_id ORDER BY r.created_at DESC").fetchall()
    by_college = {}
    for r in rows:
        by_college.setdefault(r["host_college"], []).append(
            r["course_stars"] + r["vibe_stars"])
    stats = []
    for college, scores in by_college.items():
        mean = statistics.mean(scores)
        sd = statistics.stdev(scores) if len(scores) > 1 else 0.0
        stats.append((college, mean, sd, len(scores)))
    stats.sort(key=lambda s: -s[1])
    return render_template("reviews.html", rows=rows, stats=stats)


@bp.route("/photos/<path:name>")
def photo(name):
    if "/" in name or name.startswith("."):
        abort(404)
    return send_from_directory(config.PHOTOS_DIR, name, max_age=86400)


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
