"""Site usage tracking (page views) for the admin Site usage page."""
import re
import secrets

from flask import request, session

from .db import get_db

# Not worth counting: assets, service worker, manifest, calendar feed polls.
_SKIP_ENDPOINTS = {"static", "main.manifest", "main.service_worker", "main.photo",
                   "main.calendar_feed"}
_SKIP_PREFIXES = ("/static/", "/photos/", "/favicon", "/sw.js", "/manifest")
_BOT = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|curl|wget|"
                  r"python-requests|headless|monitor", re.I)
_MOBILE = re.compile(r"Mobi|Android|iPhone|iPad", re.I)
RETENTION_DAYS = 365


def record(resp):
    """after_request hook: log one page view. Never breaks the response."""
    try:
        if request.endpoint in _SKIP_ENDPOINTS or request.path.startswith(_SKIP_PREFIXES):
            return resp
        if request.endpoint is None and resp.status_code == 404:
            route = "(not found)"
        else:
            route = request.url_rule.rule if request.url_rule else request.path[:200]
        if "vid" not in session:
            session["vid"] = secrets.token_hex(8)
        ua = request.headers.get("User-Agent", "")
        args = request.view_args or {}
        get_db().execute(
            "INSERT INTO page_views(user_id, visitor, is_admin, method, route, formal_id, "
            "status, mobile, bot) VALUES (?,?,?,?,?,?,?,?,?)",
            (session.get("uid"), session["vid"], 1 if session.get("is_admin") else 0,
             request.method, route[:200], args.get("formal_id") or args.get("fid"),
             resp.status_code, 1 if _MOBILE.search(ua) else 0,
             1 if (not ua or _BOT.search(ua)) else 0))
    except Exception:   # analytics must never take the site down
        pass
    return resp


def prune(conn):
    conn.execute(f"DELETE FROM page_views WHERE at < datetime('now', '-{RETENTION_DAYS} days')")
