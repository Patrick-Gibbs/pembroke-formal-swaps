"""PIN/password hashing (argon2id + pepper), CSRF, rate limiting, auth decorators."""
import functools
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from flask import session, request, abort, redirect, url_for, g

from . import config
from .db import get_db

_ph = PasswordHasher()  # argon2id by default, per-hash random salt


def _peppered(secret_text):
    return secret_text + config.PIN_PEPPER


def hash_secret(secret_text):
    return _ph.hash(_peppered(secret_text))


def verify_secret(stored_hash, secret_text):
    try:
        return _ph.verify(stored_hash, _peppered(secret_text))
    except (VerifyMismatchError, InvalidHashError):
        return False


# ---------------------------------------------------------------- CSRF

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def valid_csrf(sent):
    good = session.get("_csrf", "")
    return bool(good) and hmac.compare_digest(sent or "", good)


def check_csrf():
    # /chat posts JSON and validates the token itself (see views.main.chat).
    if request.method == "POST" and request.path != "/chat":
        if not valid_csrf(request.form.get("_csrf", "")):
            abort(400, "CSRF token missing or invalid.")


# ---------------------------------------------------------------- rate limiting
# Per-account: after 5 consecutive failures, lock; lock doubles with each
# further failure (15 min, 30 min, 60 min... capped at 24 h).
# Per-IP: max 20 failures in the last hour across all accounts.

ACCOUNT_THRESHOLD = 5
BASE_LOCK_MINUTES = 15
MAX_LOCK_MINUTES = 24 * 60
IP_LIMIT_PER_HOUR = 20
CHAT_LIMIT_PER_HOUR = 20      # per-IP
CHAT_GLOBAL_PER_DAY = 20     # whole platform, last 24h
CHAT_GLOBAL_PER_WEEK = 100   # whole platform, last 7 days


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # matches sqlite datetime('now')


def client_ip():
    if config.TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def consecutive_failures(conn, kind, identifier):
    rows = conn.execute(
        "SELECT success, at FROM login_attempts WHERE kind=? AND identifier=? "
        "ORDER BY id DESC LIMIT 50", (kind, identifier)).fetchall()
    n, last_at = 0, None
    for r in rows:
        if r["success"]:
            break
        if last_at is None:
            last_at = r["at"]
        n += 1
    return n, last_at


def lockout_remaining(conn, kind, identifier):
    """Seconds until this account may try again, or 0."""
    fails, last_at = consecutive_failures(conn, kind, identifier)
    if fails < ACCOUNT_THRESHOLD or last_at is None:
        return 0
    minutes = min(BASE_LOCK_MINUTES * (2 ** (fails - ACCOUNT_THRESHOLD)), MAX_LOCK_MINUTES)
    unlock = datetime.fromisoformat(last_at) + timedelta(minutes=minutes)
    return max(0, int((unlock - _utcnow()).total_seconds()))


def ip_blocked(conn, kind, ip):
    cutoff = (_utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM login_attempts "
        "WHERE kind=? AND identifier=? AND success=0 AND at > ?",
        (kind, "ip:" + ip, cutoff)).fetchone()
    return row["n"] >= IP_LIMIT_PER_HOUR


def _chat_count_since(conn, delta):
    cutoff = (_utcnow() - delta).strftime("%Y-%m-%d %H:%M:%S")
    return conn.execute(
        "SELECT COUNT(*) AS n FROM login_attempts WHERE kind='chat' AND at > ?",
        (cutoff,)).fetchone()["n"]


def chat_global_limited(conn):
    """True if the whole platform has hit the daily (last 24h) or weekly
    (last 7 days) chatbot cap. Count only — does not log."""
    return (_chat_count_since(conn, timedelta(days=1)) >= CHAT_GLOBAL_PER_DAY
            or _chat_count_since(conn, timedelta(days=7)) >= CHAT_GLOBAL_PER_WEEK)


def chat_rate_limited(conn, ip):
    """True if this IP has hit the hourly chatbot cap. Logs the request either
    way (reusing the login_attempts table with kind='chat')."""
    cutoff = (_utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM login_attempts "
        "WHERE kind='chat' AND identifier=? AND at > ?",
        ("ip:" + ip, cutoff)).fetchone()
    conn.execute("INSERT INTO login_attempts(kind, identifier, success) "
                 "VALUES ('chat', ?, 1)", ("ip:" + ip,))
    return row["n"] >= CHAT_LIMIT_PER_HOUR


def record_attempt(conn, kind, identifier, ip, success):
    conn.execute("INSERT INTO login_attempts(kind, identifier, success) VALUES (?,?,?)",
                 (kind, identifier, int(success)))
    conn.execute("INSERT INTO login_attempts(kind, identifier, success) VALUES (?,?,?)",
                 (kind, "ip:" + ip, int(success)))


# ---------------------------------------------------------------- auth decorators

def current_user():
    if "user" in g:
        return g.user
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id=? AND email_verified=1", (uid,)).fetchone()
    return g.user


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if current_user() is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if not session.get("is_admin"):
            return redirect(url_for("admin.login"))
        return view(*a, **kw)
    return wrapped
