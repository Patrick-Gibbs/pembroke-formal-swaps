"""Per-term settings. Each term keeps its own ballot window and published state
under '<key>@<term>'. The plain global keys mirror the *current* term (the one
members see), so member-facing code can keep reading them unchanged; making a
term current copies its values into the globals."""
import json

from .db import audit, get_setting, set_setting

TERM_KEYS = ("results_published", "term_ballot_open", "term_ballot_close")
# What a term has before anything is set: a brand-new term is unpublished with
# no window; terms that predate per-term storage were published.
NEW_TERM_DEFAULTS = {"results_published": "0", "term_ballot_open": "",
                     "term_ballot_close": ""}


def current_term(conn):
    return get_setting(conn, "current_term", "")


def term_setting(conn, key, term, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (f"{key}@{term}",)).fetchone()
    if row is not None:
        return row["value"]
    if term and term == current_term(conn):
        return get_setting(conn, key, default)
    return default


def set_term_setting(conn, key, term, value):
    set_setting(conn, f"{key}@{term}", value)
    if term == current_term(conn):
        set_setting(conn, key, value)


def is_published(conn, term):
    return term_setting(conn, "results_published", term, "1") == "1"


def all_terms(conn):
    """Every known term, newest first: those with formals, those created here
    (possibly still empty) and the current one."""
    seen = {}
    for r in conn.execute("SELECT term, MIN(dt) AS first FROM formals "
                          "WHERE term != '' GROUP BY term"):
        seen[r["term"]] = r["first"]
    try:
        created = json.loads(get_setting(conn, "terms", "[]") or "[]")
    except ValueError:
        created = []
    for t in created + [current_term(conn)]:
        if t and t not in seen:
            seen[t] = "9999"           # no formals yet: list first
    return sorted(seen, key=lambda t: seen[t], reverse=True)


def create_term(conn, name):
    """Register a new (empty) term with fresh, unpublished settings."""
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Give the term a name.")
    if name in all_terms(conn):
        raise ValueError(f"“{name}” already exists.")
    try:
        created = json.loads(get_setting(conn, "terms", "[]") or "[]")
    except ValueError:
        created = []
    set_setting(conn, "terms", json.dumps(created + [name]))
    for key, val in NEW_TERM_DEFAULTS.items():
        set_setting(conn, f"{key}@{name}", val)
    audit(conn, "admin", "term_create", name)
    return name


def make_current(conn, term):
    """Make `term` the one members see. The outgoing term keeps its values."""
    old = current_term(conn)
    if old == term:
        return
    incoming = {}
    for key in TERM_KEYS:      # read before switching (lookups fall back to globals)
        row = conn.execute("SELECT value FROM settings WHERE key=?",
                           (f"{key}@{term}",)).fetchone()
        incoming[key] = row["value"] if row is not None else None
    if incoming["results_published"] is None:   # older term: published if emailed
        sent = conn.execute(
            "SELECT 1 FROM allocations a JOIN formals f ON f.id=a.formal_id "
            "WHERE f.term=? AND a.notified=1 LIMIT 1", (term,)).fetchone()
        incoming["results_published"] = "1" if sent else "0"
    if old:
        for key in TERM_KEYS:
            set_setting(conn, f"{key}@{old}", get_setting(conn, key, ""))
    set_setting(conn, "current_term", term)
    for key in TERM_KEYS:
        set_term_setting(conn, key, term, incoming[key] if incoming[key] is not None
                         else NEW_TERM_DEFAULTS[key])
    audit(conn, "admin", "term_rollover", f"current_term={term} (was {old})")
