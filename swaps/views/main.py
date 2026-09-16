import os
import secrets
import statistics
import threading

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   send_from_directory, url_for)

from .. import chatbot, config
from ..db import get_db, get_setting, audit
from ..security import (chat_global_limited, chat_rate_limited, client_ip,
                        current_user, login_required)
from ..services import (CancelError, ClaimError, cancel_allocation,
                        cancel_cutoff_hours, claim_seat, free_seats,
                        hours_until_formal, local_now, parse_local)

PHOTO_MAX_DIM = 2000   # longest edge, px — larger uploads are downscaled
MAX_REVIEW_PHOTOS = 3


def _review_photos(db, review_id):
    return [r["filename"] for r in db.execute(
        "SELECT filename FROM review_photos WHERE review_id=? ORDER BY id",
        (review_id,)).fetchall()]


def _delete_photo(name):
    try:
        os.remove(os.path.join(config.PHOTOS_DIR, name))
    except OSError:
        pass


def _save_review_photo(file, formal_id, user_id):
    """Read an uploaded image, fix EXIF orientation, downscale very large
    photos, and save as a JPEG. Returns the filename, or None if the file
    isn't a readable image. Never rejects for being too big — it shrinks."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        img = Image.open(file.stream)
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError):
        return None
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((PHOTO_MAX_DIM, PHOTO_MAX_DIM))  # in-place, keeps aspect ratio
    os.makedirs(config.PHOTOS_DIR, exist_ok=True)
    name = f"r{formal_id}-{user_id}-{secrets.token_hex(4)}.jpg"
    img.save(os.path.join(config.PHOTOS_DIR, name), "JPEG", quality=85, optimize=True)
    return name

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
    user = current_user()
    attending = set()
    if user:
        attending = {r["formal_id"] for r in db.execute(
            "SELECT formal_id FROM allocations WHERE user_id=? AND status='active'",
            (user["id"],)).fetchall()}
    return render_template("index.html", formals=formals, term=term,
                           attending=attending,
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


# CPU guard for the single-core VM: the simulation is CPU-bound, so cap it at
# one at a time. During a stampede (many users clicking near ballot close) extra
# requests get an instant "busy" instead of piling work onto the one core.
_SIM_SEMAPHORE = threading.BoundedSemaphore(1)


@bp.route("/simulate")
@login_required
def simulate():
    from flask import jsonify
    from ..services import simulate_user
    db = get_db()
    term = get_setting(db, "current_term")
    if not term:
        return jsonify({"error": "No term is set up."}), 400
    if not _SIM_SEMAPHORE.acquire(blocking=False):
        return jsonify({"error": "The simulator is busy right now — please try "
                        "again in a few seconds."}), 429
    try:
        result = simulate_user(db, current_user()["id"], term, trials=400)
    finally:
        _SIM_SEMAPHORE.release()
    if result is None:
        return jsonify({"error": "Rank at least one formal (and save) first, "
                        "then simulate."}), 400
    return jsonify(result)


@bp.route("/simulate/pair")
@login_required
def simulate_pair_route():
    from flask import jsonify
    from ..services import simulate_pair
    db = get_db()
    term = get_setting(db, "current_term")
    if not term:
        return jsonify({"error": "No term is set up."}), 400
    try:
        other_id = int(request.args.get("with", ""))
    except ValueError:
        return jsonify({"error": "Pick a person from the suggestions."}), 400
    if other_id == current_user()["id"]:
        return jsonify({"error": "Pick someone other than yourself."}), 400
    if db.execute("SELECT 1 FROM users WHERE id=? AND email_verified=1",
                  (other_id,)).fetchone() is None:
        return jsonify({"error": "No such registered member."}), 400
    if not _SIM_SEMAPHORE.acquire(blocking=False):
        return jsonify({"error": "The simulator is busy right now — please try "
                        "again in a few seconds."}), 429
    try:
        result = simulate_pair(db, current_user()["id"], other_id, term, trials=400)
    finally:
        _SIM_SEMAPHORE.release()
    if result is None:
        return jsonify({"error": "Rank at least one formal (and save) first, "
                        "then simulate."}), 400
    return jsonify(result)


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
    # Public list shows names (and party) only — dietary is admin-only and never
    # exposed here; admins get it on the roster pages and exports.
    data = []
    for f in formals:
        rows = db.execute(
            "SELECT u.first_name, u.last_name, COALESCE(g.party_name, '') AS party "
            "FROM allocations a JOIN users u ON u.id = a.user_id "
            "LEFT JOIN ballot_group_members m ON m.user_id = u.id "
            "  AND m.status = 'accepted' "
            "LEFT JOIN ballot_groups g ON g.id = m.group_id AND g.term = ? "
            "WHERE a.formal_id=? AND a.status='active' "
            "ORDER BY u.last_name, u.first_name", (term, f["id"])).fetchall()
        data.append((f, rows))
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
    reviewed_ids = {r["formal_id"] for r in db.execute(
        "SELECT formal_id FROM reviews WHERE user_id=?", (user["id"],)).fetchall()}
    cal_url = f"{config.SITE_URL}/calendar/{_calendar_token(db, user)}.ics"

    # Profile/history stats (merged in from the old /profile page).
    active = [a for a in allocs if a["status"] == "active"]
    reviews = db.execute(
        "SELECT r.course_stars, r.vibe_stars, r.review, r.created_at, "
        "f.host_college, f.dt FROM reviews r JOIN formals f ON f.id=r.formal_id "
        "WHERE r.user_id=? ORDER BY r.created_at DESC", (user["id"],)).fetchall()
    stars = [r["course_stars"] + r["vibe_stars"] for r in reviews]
    from collections import Counter
    fav = Counter(a["host_college"] for a in active).most_common(1)
    stats = {
        "attended": sum(1 for a in active if hours_until_formal(a["dt"]) < 0),
        "upcoming": sum(1 for a in active if hours_until_formal(a["dt"]) >= 0),
        "reviews": len(reviews),
        "avg_given": round(sum(stars) / len(stars), 1) if stars else None,
        "favourite": fav[0][0] if fav else None,
    }
    return render_template("me.html", allocs=allocs, subs=subs,
                           cancellable=cancellable, past=past, reviewed_ids=reviewed_ids,
                           cutoff_h=int(cutoff), cal_url=cal_url,
                           reviews=reviews, stats=stats)


@bp.route("/profile")
@login_required
def profile():
    return redirect(url_for("main.me"))  # merged into My formals


@bp.route("/robots.txt")
def robots():
    from flask import Response
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@bp.route("/privacy")
def privacy():
    return render_template("privacy.html")


def _calendar_token(db, user):
    """Return the user's private calendar-feed token, generating it on first use."""
    if user["calendar_token"]:
        return user["calendar_token"]
    token = secrets.token_urlsafe(24)
    db.execute("UPDATE users SET calendar_token=? WHERE id=?", (token, user["id"]))
    return token


@bp.route("/calendar/<token>.ics")
def calendar_feed(token):
    from flask import Response
    from ..ics import feed_ics
    db = get_db()
    user = db.execute("SELECT id FROM users WHERE calendar_token=?", (token,)).fetchone()
    if user is None:
        abort(404)
    formals = db.execute(
        "SELECT f.* FROM allocations a JOIN formals f ON f.id=a.formal_id "
        "WHERE a.user_id=? AND a.status='active' ORDER BY f.dt", (user["id"],)).fetchall()
    return Response(feed_ics(formals), mimetype="text/calendar", headers={
        "Content-Disposition": "inline; filename=formal-swaps.ics"})


@bp.route("/account/export.json")
@login_required
def account_export():
    from flask import Response
    import json
    from ..services import export_user_data
    data = export_user_data(get_db(), current_user()["id"])
    body = json.dumps({"exported_at": local_now().isoformat(),
                       "site": config.SITE_URL, "your_data": data},
                      indent=2, default=str)
    return Response(body, mimetype="application/json", headers={
        "Content-Disposition": "attachment; filename=my-formal-swaps-data.json"})


@bp.route("/account/delete", methods=["POST"])
@login_required
def account_delete():
    from flask import session
    from ..security import verify_secret
    from ..services import delete_user_data
    db = get_db()
    user = current_user()
    if not verify_secret(user["pin_hash"], request.form.get("pin", "")):
        flash("Incorrect PIN — account not deleted.", "error")
        return redirect(url_for("main.me"))
    photos = delete_user_data(db, user["id"])
    for name in photos:
        try:
            os.remove(os.path.join(config.PHOTOS_DIR, name))
        except OSError:
            pass
    session.clear()
    flash("Your account and personal data have been permanently deleted.", "ok")
    return redirect(url_for("main.index"))


def _swaps_published(db):
    return get_setting(db, "results_published", "1") == "1"


@bp.route("/swaps")
@login_required
def swaps():
    db = get_db()
    uid = current_user()["id"]
    published = _swaps_published(db)
    mine = db.execute(
        "SELECT f.id AS formal_id, f.host_college, f.dt FROM allocations a "
        "JOIN formals f ON f.id=a.formal_id WHERE a.user_id=? AND a.status='active' "
        "ORDER BY f.dt", (uid,)).fetchall()

    def hydrate(rows):
        out = []
        for r in rows:
            ff = db.execute("SELECT host_college, dt FROM formals WHERE id=?",
                            (r["from_formal"],)).fetchone()
            tf = db.execute("SELECT host_college, dt FROM formals WHERE id=?",
                            (r["to_formal"],)).fetchone()
            fu = db.execute("SELECT first_name, last_name FROM users WHERE id=?",
                            (r["from_user"],)).fetchone()
            tu = db.execute("SELECT first_name, last_name FROM users WHERE id=?",
                            (r["to_user"],)).fetchone()
            out.append({"id": r["id"], "from_formal": ff, "to_formal": tf,
                        "from_name": f"{fu['first_name']} {fu['last_name']}" if fu else "?",
                        "to_name": f"{tu['first_name']} {tu['last_name']}" if tu else "?"})
        return out

    incoming = hydrate(db.execute(
        "SELECT * FROM swap_requests WHERE to_user=? AND status='pending' "
        "ORDER BY id DESC", (uid,)).fetchall())
    outgoing = hydrate(db.execute(
        "SELECT * FROM swap_requests WHERE from_user=? AND status='pending' "
        "ORDER BY id DESC", (uid,)).fetchall())
    return render_template("swaps.html", published=published, mine=mine,
                           incoming=incoming, outgoing=outgoing)


@bp.route("/swaps/holdings")
@login_required
def swaps_holdings():
    from flask import jsonify
    db = get_db()
    if not _swaps_published(db):
        return jsonify([])
    try:
        other = int(request.args.get("user", ""))
    except ValueError:
        return jsonify([])
    if other == current_user()["id"]:
        return jsonify([])
    rows = db.execute(
        "SELECT f.id, f.host_college, f.dt FROM allocations a "
        "JOIN formals f ON f.id=a.formal_id WHERE a.user_id=? AND a.status='active' "
        "ORDER BY f.dt", (other,)).fetchall()
    return jsonify([{"formal_id": r["id"],
                     "label": f"{r['host_college']} — {r['dt']}"} for r in rows])


@bp.route("/swaps/propose", methods=["POST"])
@login_required
def swaps_propose():
    from ..services import SwapError, propose_swap
    from .. import emailer
    db = get_db()
    uid = current_user()["id"]
    if not _swaps_published(db):
        flash("Swaps open once results are published.", "error")
        return redirect(url_for("main.swaps"))
    try:
        from_formal = int(request.form.get("from_formal", ""))
        to_user = int(request.form.get("to_user", ""))
        to_formal = int(request.form.get("to_formal", ""))
    except ValueError:
        flash("Choose your place, a person, and the place you want.", "error")
        return redirect(url_for("main.swaps"))
    try:
        propose_swap(db, uid, from_formal, to_user, to_formal)
    except SwapError as e:
        flash(str(e), "error")
        return redirect(url_for("main.swaps"))
    target = db.execute("SELECT email FROM users WHERE id=?", (to_user,)).fetchone()
    offer = db.execute("SELECT host_college, dt FROM formals WHERE id=?",
                       (from_formal,)).fetchone()
    want = db.execute("SELECT host_college, dt FROM formals WHERE id=?",
                      (to_formal,)).fetchone()
    me = current_user()
    if target:
        emailer.swap_proposed_email(
            target["email"], f"{me['first_name']} {me['last_name']}",
            offer, want, f"{config.SITE_URL}/swaps")
    flash("Swap request sent.", "ok")
    return redirect(url_for("main.swaps"))


@bp.route("/swaps/<int:req_id>/accept", methods=["POST"])
@login_required
def swaps_accept(req_id):
    from ..services import SwapError, accept_swap
    from .. import emailer
    db = get_db()
    uid = current_user()["id"]
    try:
        r = accept_swap(db, req_id, uid)
    except SwapError as e:
        flash(str(e), "error")
        return redirect(url_for("main.swaps"))
    # Notify both parties of their new/relinquished places.
    ff = db.execute("SELECT host_college, dt FROM formals WHERE id=?",
                    (r["from_formal"],)).fetchone()
    tf = db.execute("SELECT host_college, dt FROM formals WHERE id=?",
                    (r["to_formal"],)).fetchone()
    fu = db.execute("SELECT first_name, last_name, email FROM users WHERE id=?",
                    (r["from_user"],)).fetchone()
    tu = db.execute("SELECT first_name, last_name, email FROM users WHERE id=?",
                    (r["to_user"],)).fetchone()
    if fu:  # proposer now attends to_formal, gave up from_formal
        emailer.swap_accepted_email(fu["email"], f"{tu['first_name']} {tu['last_name']}",
                                    tf, ff)
    if tu:  # accepter now attends from_formal, gave up to_formal
        emailer.swap_accepted_email(tu["email"], f"{fu['first_name']} {fu['last_name']}",
                                    ff, tf)
    flash("Swap complete — your places have been exchanged.", "ok")
    return redirect(url_for("main.swaps"))


@bp.route("/swaps/<int:req_id>/<any(decline,cancel):action>", methods=["POST"])
@login_required
def swaps_respond(req_id, action):
    from ..services import SwapError, respond_swap
    db = get_db()
    try:
        respond_swap(db, req_id, current_user()["id"], action)
        flash("Swap request " + ("declined." if action == "decline" else "cancelled."),
              "ok")
    except SwapError as e:
        flash(str(e), "error")
    return redirect(url_for("main.swaps"))


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
    opens = parse_local(f["dt"]).replace(hour=9, minute=0, second=0, microsecond=0)
    if local_now() < opens:
        flash("Reviews open at 9am on the day of the formal.", "error")
        return redirect(url_for("main.me"))

    existing = db.execute("SELECT * FROM reviews WHERE user_id=? AND formal_id=?",
                          (user["id"], formal_id)).fetchone()
    if request.method == "POST":
        course_stars = sum(1 for c in ("course1", "course2", "course3")
                           if request.form.get(c))
        vibe_stars = sum(1 for v in ("vibe_hosts", "vibe_college")
                         if request.form.get(v))
        text = request.form.get("review", "").strip()[:2000]

        # New photos (up to 3) replace any existing ones; uploading none keeps
        # the current photos. Oversized images are downscaled, not rejected.
        files = [x for x in request.files.getlist("photos") if x and x.filename]
        saved = []
        for x in files[:MAX_REVIEW_PHOTOS]:
            new_name = _save_review_photo(x, formal_id, user["id"])
            if new_name is None:
                for n in saved:  # clean up partial batch
                    _delete_photo(n)
                flash("Couldn't read one of those photos — please upload normal "
                      "images (JPEG, PNG, etc.).", "error")
                return render_template("review_form.html", f=f, r=existing,
                                       photos=_review_photos(db, existing["id"])
                                       if existing else [])
            saved.append(new_name)

        db.execute(
            "INSERT INTO reviews(user_id, formal_id, course_stars, vibe_stars, "
            "review) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id, formal_id) DO UPDATE SET course_stars=excluded."
            "course_stars, vibe_stars=excluded.vibe_stars, review=excluded.review, "
            "created_at=datetime('now')",
            (user["id"], formal_id, course_stars, vibe_stars, text))
        review_id = db.execute("SELECT id FROM reviews WHERE user_id=? AND formal_id=?",
                               (user["id"], formal_id)).fetchone()["id"]
        if saved:  # replace existing photos with the new batch
            for old in _review_photos(db, review_id):
                _delete_photo(old)
            db.execute("DELETE FROM review_photos WHERE review_id=?", (review_id,))
            for name in saved:
                db.execute("INSERT INTO review_photos(review_id, filename) "
                           "VALUES (?,?)", (review_id, name))
        flash(f"Thanks — you rated it {course_stars + vibe_stars}/5. "
              "You can edit your review any time.", "ok")
        return redirect(url_for("main.reviews_page"))
    return render_template("review_form.html", f=f, r=existing,
                           photos=_review_photos(db, existing["id"]) if existing else [])


def _college_stats(db):
    """Per-college rating stats joined to endowment, for the charts + table."""
    rows = db.execute(
        "SELECT f.host_college AS college, r.course_stars + r.vibe_stars AS score "
        "FROM reviews r JOIN formals f ON f.id = r.formal_id").fetchall()
    endow = {r["college"]: r["endowment_m"] for r in db.execute(
        "SELECT college, endowment_m FROM college_endowments")}
    by_college = {}
    for r in rows:
        by_college.setdefault(r["college"], []).append(r["score"])
    out = []
    for college, scores in by_college.items():
        n = len(scores)
        mean = statistics.mean(scores)
        sd = statistics.stdev(scores) if n > 1 else 0.0
        se = sd / (n ** 0.5) if n > 1 else 0.0
        out.append({"college": college, "n": n, "mean": round(mean, 3),
                    "sd": round(sd, 3), "se": round(se, 3),
                    "endowment_m": endow.get(college)})
    return out


@bp.route("/reviews")
def reviews_page():
    db = get_db()
    rows = db.execute(
        "SELECT r.*, f.host_college, f.dt, u.first_name, u.last_name "
        "FROM reviews r JOIN formals f ON f.id = r.formal_id "
        "JOIN users u ON u.id = r.user_id ORDER BY r.created_at DESC").fetchall()
    photos = {r["id"]: _review_photos(db, r["id"]) for r in rows}
    stats = [(s["college"], s["mean"], s["sd"], s["n"])
             for s in sorted(_college_stats(db), key=lambda s: -s["mean"])]
    return render_template("reviews.html", rows=rows, stats=stats, photos=photos)


@bp.route("/reviews/data.json")
def reviews_data():
    from flask import jsonify
    return jsonify(_college_stats(get_db()))


@bp.route("/chat", methods=["POST"])
def chat():
    from flask import jsonify
    from ..security import valid_csrf
    data = request.get_json(silent=True) or {}
    if not valid_csrf(data.get("_csrf", "")):
        return jsonify({"error": "Invalid session token — reload the page."}), 400
    if not chatbot.is_available():
        return jsonify({"reply": "The chatbot isn't available right now."})
    message = (data.get("message") or "").strip()[:chatbot.MAX_MESSAGE_CHARS]
    if not message:
        return jsonify({"error": "Please enter a question."}), 400
    # Sanitise short client-supplied history: alternating roles, capped length.
    history = []
    for turn in (data.get("history") or [])[-chatbot.MAX_HISTORY_TURNS:]:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()[:chatbot.MAX_MESSAGE_CHARS]
        if role in ("user", "assistant") and content:
            history.append({"role": role, "content": content})

    db = get_db()
    if chat_global_limited(db):
        return jsonify({"reply": "The chat assistant has reached its usage limit "
                        "for now — please check the formals list on the home "
                        "page, or try again later."}), 429
    if chat_rate_limited(db, client_ip()):
        return jsonify({"reply": "You've reached the hourly limit for the chat "
                        "assistant — please try again later."}), 429
    return jsonify({"reply": chatbot.answer(db, message, history)})


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
