"""Group balloting: a leader forms a group, invites registered members by
name, invitees accept/decline; the leader's ranking then enters the ballot as
one block that wins or loses together."""
from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, url_for)

from .. import config, emailer
from ..db import get_db, get_setting, audit
from ..security import current_user, login_required

bp = Blueprint("ballot", __name__, url_prefix="/ballot")

MAX_GROUP_SIZE = 6


def _term(db):
    return get_setting(db, "current_term")


def accepted_group(db, uid, term):
    """The group (row) this user is an accepted member of this term, or None."""
    return db.execute(
        "SELECT g.* FROM ballot_groups g JOIN ballot_group_members m "
        "ON m.group_id = g.id WHERE m.user_id=? AND m.status='accepted' AND g.term=?",
        (uid, term)).fetchone()


def group_members(db, gid):
    return db.execute(
        "SELECT m.id AS mid, m.status, u.id AS user_id, u.first_name, u.last_name "
        "FROM ballot_group_members m JOIN users u ON u.id = m.user_id "
        "WHERE m.group_id=? ORDER BY m.status = 'accepted' DESC, m.invited_at",
        (gid,)).fetchall()


@bp.route("/")
@login_required
def home():
    db = get_db()
    user = current_user()
    term = _term(db)
    group = accepted_group(db, user["id"], term) if term else None
    members = group_members(db, group["id"]) if group else []
    invites = db.execute(
        "SELECT m.id AS mid, g.id AS gid, u.first_name, u.last_name "
        "FROM ballot_group_members m JOIN ballot_groups g ON g.id = m.group_id "
        "JOIN users u ON u.id = g.leader_user_id "
        "WHERE m.user_id=? AND m.status='invited' AND g.term=?",
        (user["id"], term)).fetchall() if term else []
    n_accepted = sum(1 for m in members if m["status"] == "accepted")
    return render_template(
        "ballot.html", term=term, group=group, members=members, invites=invites,
        is_leader=bool(group and group["leader_user_id"] == user["id"]),
        n_accepted=n_accepted, max_size=MAX_GROUP_SIZE)


@bp.route("/create", methods=["POST"])
@login_required
def create():
    db = get_db()
    user = current_user()
    term = _term(db)
    if not term:
        flash("No term is set up yet.", "error")
        return redirect(url_for("ballot.home"))
    if accepted_group(db, user["id"], term):
        flash("You're already in a group for this term.", "error")
        return redirect(url_for("ballot.home"))
    cur = db.execute("INSERT INTO ballot_groups(term, leader_user_id) VALUES (?,?)",
                     (term, user["id"]))
    db.execute("INSERT INTO ballot_group_members(group_id, user_id, status, "
               "responded_at) VALUES (?,?,'accepted',datetime('now'))",
               (cur.lastrowid, user["id"]))
    audit(db, f"user:{user['id']}", "group_create", f"group={cur.lastrowid} term={term}")
    flash("Group created — invite people below. Your ranking on the "
          "“My ranking” page will count for the whole group.", "ok")
    return redirect(url_for("ballot.home"))


@bp.route("/search")
@login_required
def search():
    db = get_db()
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    rows = db.execute(
        "SELECT id, first_name, last_name FROM users WHERE email_verified=1 "
        "AND id != ? AND (first_name || ' ' || last_name) LIKE ? "
        "ORDER BY last_name LIMIT 8", (current_user()["id"], like)).fetchall()
    return jsonify([{"id": r["id"],
                     "name": f"{r['first_name']} {r['last_name']}"} for r in rows])


@bp.route("/invite", methods=["POST"])
@login_required
def invite():
    db = get_db()
    user = current_user()
    term = _term(db)
    group = accepted_group(db, user["id"], term)
    if not group or group["leader_user_id"] != user["id"]:
        flash("Only the group leader can invite people.", "error")
        return redirect(url_for("ballot.home"))
    try:
        target_id = int(request.form.get("user_id", ""))
    except ValueError:
        flash("Pick a person from the suggestions list.", "error")
        return redirect(url_for("ballot.home"))
    target = db.execute("SELECT * FROM users WHERE id=? AND email_verified=1",
                        (target_id,)).fetchone()
    if target is None or target_id == user["id"]:
        flash("No such member.", "error")
        return redirect(url_for("ballot.home"))
    if accepted_group(db, target_id, term):
        flash(f"{target['first_name']} is already in a group this term.", "error")
        return redirect(url_for("ballot.home"))
    n_current = db.execute(
        "SELECT COUNT(*) n FROM ballot_group_members WHERE group_id=? "
        "AND status IN ('accepted','invited')", (group["id"],)).fetchone()["n"]
    if n_current >= MAX_GROUP_SIZE:
        flash(f"Groups are capped at {MAX_GROUP_SIZE} people "
              "(bigger blocks can rarely be seated together).", "error")
        return redirect(url_for("ballot.home"))
    existing = db.execute(
        "SELECT * FROM ballot_group_members WHERE group_id=? AND user_id=?",
        (group["id"], target_id)).fetchone()
    if existing and existing["status"] == "invited":
        flash("Already invited — waiting for them to respond.", "error")
        return redirect(url_for("ballot.home"))
    if existing:  # previously declined: re-invite
        db.execute("UPDATE ballot_group_members SET status='invited', "
                   "invited_at=datetime('now'), responded_at=NULL WHERE id=?",
                   (existing["id"],))
    else:
        db.execute("INSERT INTO ballot_group_members(group_id, user_id) VALUES (?,?)",
                   (group["id"], target_id))
    emailer.group_invite_email(
        target["email"], f"{user['first_name']} {user['last_name']}", term,
        f"{config.SITE_URL}{url_for('ballot.home')}")
    audit(db, f"user:{user['id']}", "group_invite",
          f"group={group['id']} invitee={target_id}")
    flash(f"Invitation sent to {target['first_name']} {target['last_name']}.", "ok")
    return redirect(url_for("ballot.home"))


@bp.route("/respond/<int:mid>", methods=["POST"])
@login_required
def respond(mid):
    db = get_db()
    user = current_user()
    row = db.execute(
        "SELECT m.*, g.term, g.leader_user_id FROM ballot_group_members m "
        "JOIN ballot_groups g ON g.id = m.group_id WHERE m.id=? AND m.user_id=? "
        "AND m.status='invited'", (mid, user["id"])).fetchone()
    if row is None:
        abort(404)
    action = request.form.get("action", "")
    if action == "accept":
        if accepted_group(db, user["id"], row["term"]):
            flash("You're already in a group this term — leave it first.", "error")
            return redirect(url_for("ballot.home"))
        db.execute("UPDATE ballot_group_members SET status='accepted', "
                   "responded_at=datetime('now') WHERE id=?", (mid,))
        audit(db, f"user:{user['id']}", "group_accept", f"group={row['group_id']}")
        flash("You've joined the group — the leader's ranking now covers you too.", "ok")
    else:
        db.execute("UPDATE ballot_group_members SET status='declined', "
                   "responded_at=datetime('now') WHERE id=?", (mid,))
        flash("Invitation declined.", "ok")
    return redirect(url_for("ballot.home"))


@bp.route("/leave", methods=["POST"])
@login_required
def leave():
    db = get_db()
    user = current_user()
    term = _term(db)
    group = accepted_group(db, user["id"], term)
    if not group:
        return redirect(url_for("ballot.home"))
    if group["leader_user_id"] == user["id"]:  # leader leaving disbands the group
        db.execute("DELETE FROM ballot_group_members WHERE group_id=?", (group["id"],))
        db.execute("DELETE FROM ballot_groups WHERE id=?", (group["id"],))
        audit(db, f"user:{user['id']}", "group_disband", f"group={group['id']}")
        flash("Group disbanded.", "ok")
    else:
        db.execute("DELETE FROM ballot_group_members WHERE group_id=? AND user_id=?",
                   (group["id"], user["id"]))
        audit(db, f"user:{user['id']}", "group_leave", f"group={group['id']}")
        flash("You've left the group — you now ballot individually.", "ok")
    return redirect(url_for("ballot.home"))


@bp.route("/remove/<int:mid>", methods=["POST"])
@login_required
def remove(mid):
    db = get_db()
    user = current_user()
    row = db.execute(
        "SELECT m.*, g.leader_user_id FROM ballot_group_members m "
        "JOIN ballot_groups g ON g.id = m.group_id WHERE m.id=?", (mid,)).fetchone()
    if (row is None or row["leader_user_id"] != user["id"]
            or row["user_id"] == user["id"]):
        abort(404)
    db.execute("DELETE FROM ballot_group_members WHERE id=?", (mid,))
    audit(db, f"user:{user['id']}", "group_remove",
          f"group={row['group_id']} member={row['user_id']}")
    flash("Removed.", "ok")
    return redirect(url_for("ballot.home"))
