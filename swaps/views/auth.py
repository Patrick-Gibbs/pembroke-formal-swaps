import re

from flask import (Blueprint, flash, redirect, render_template, request,
                   session, url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .. import config, emailer
from ..db import get_db, audit
from ..security import (client_ip, hash_secret, ip_blocked, lockout_remaining,
                        record_attempt, verify_secret)

bp = Blueprint("auth", __name__)

DIETARY_CHOICES = ["Vegetarian", "Vegan", "Gluten-free", "Dairy-free", "Nut allergy",
                   "Halal", "Kosher", "Pescatarian"]

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)*cam\.ac\.uk$")
PIN_RE = re.compile(r"^\d{4}$")


def _serializer():
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="email-verify")


def _send_verification(user_id, email):
    token = _serializer().dumps({"uid": user_id})
    link = f"{config.SITE_URL}{url_for('auth.verify', token=token)}"
    emailer.verification_email(email, link)


@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        first = request.form.get("first_name", "").strip()[:80]
        last = request.form.get("last_name", "").strip()[:80]
        email = request.form.get("email", "").strip().lower()
        pin = request.form.get("pin", "")
        flags = [c for c in DIETARY_CHOICES if request.form.get("diet_" + c)]
        other = request.form.get("dietary_other", "").strip()[:500]

        errors = []
        if not first or not last:
            errors.append("Name is required.")
        if not EMAIL_RE.match(email):
            errors.append("Email must be a cam.ac.uk address "
                          "(e.g. crsid@pem.cam.ac.uk or any *.cam.ac.uk).")
        if not PIN_RE.match(pin):
            errors.append("PIN must be exactly 4 digits.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html", form=request.form,
                                   dietary_choices=DIETARY_CHOICES)

        db = get_db()
        existing = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if existing and existing["email_verified"]:
            flash("That email is already registered — log in instead.", "error")
            return redirect(url_for("auth.login"))
        if existing:  # unverified: allow re-register (overwrites, resends link)
            db.execute("UPDATE users SET first_name=?, last_name=?, pin_hash=?, "
                       "dietary_flags=?, dietary_other=? WHERE id=?",
                       (first, last, hash_secret(pin), ",".join(flags), other,
                        existing["id"]))
            uid = existing["id"]
        else:
            cur = db.execute(
                "INSERT INTO users(first_name, last_name, email, pin_hash, "
                "dietary_flags, dietary_other) VALUES (?,?,?,?,?,?)",
                (first, last, email, hash_secret(pin), ",".join(flags), other))
            uid = cur.lastrowid
        _send_verification(uid, email)
        return render_template("verify_pending.html", email=email)
    return render_template("register.html", form={}, dietary_choices=DIETARY_CHOICES)


@bp.route("/verify/<token>")
def verify(token):
    try:
        data = _serializer().loads(token, max_age=config.VERIFY_TOKEN_MAX_AGE)
    except SignatureExpired:
        flash("That verification link has expired — register again to get a new one.",
              "error")
        return redirect(url_for("auth.register"))
    except BadSignature:
        flash("Invalid verification link.", "error")
        return redirect(url_for("auth.register"))
    db = get_db()
    db.execute("UPDATE users SET email_verified=1 WHERE id=?", (data["uid"],))
    flash("Email verified — you can now log in.", "ok")
    return redirect(url_for("auth.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pin = request.form.get("pin", "")
        db = get_db()
        ip = client_ip()

        if ip_blocked(db, "user", ip):
            flash("Too many failed attempts from your network. Try again later.", "error")
            return render_template("login.html"), 429
        wait = lockout_remaining(db, "user", email)
        if wait:
            flash(f"Account temporarily locked. Try again in {wait // 60 + 1} minutes.",
                  "error")
            return render_template("login.html"), 429

        row = db.execute("SELECT * FROM users WHERE email=? AND email_verified=1",
                         (email,)).fetchone()
        ok = row is not None and verify_secret(row["pin_hash"], pin)
        record_attempt(db, "user", email, ip, ok)
        if not ok:
            flash("Wrong email or PIN.", "error")
            return render_template("login.html"), 401
        session.clear()
        session["uid"] = row["id"]
        session.permanent = True
        nxt = request.args.get("next", "")
        return redirect(nxt if nxt.startswith("/") else url_for("main.index"))
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("main.index"))
