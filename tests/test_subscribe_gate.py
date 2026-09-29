"""'Notify me' subscriptions are only accepted once the ballot has run for the
formal (status 'allocated') and results are published."""
import importlib

from conftest import add_formal, add_user
from swaps.db import connect, set_setting


def _client(db_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["_csrf"] = "tok"
        s["uid"] = 1
    return c


def _subs(db_path):
    conn = connect(db_path)
    n = conn.execute("SELECT COUNT(*) n FROM subscriptions").fetchone()["n"]
    conn.close()
    return n


def test_subscribe_only_after_allocation_and_publish(db_path, monkeypatch):
    conn = connect(db_path)
    add_user(conn, 1)
    add_formal(conn, 1, slots=2)
    conn.execute("UPDATE formals SET status='open'")  # pre-ballot
    set_setting(conn, "results_published", "1")
    conn.commit(); conn.close()
    c = _client(db_path, monkeypatch)

    c.post("/formals/1/subscribe", data={"_csrf": "tok"})
    assert _subs(db_path) == 0                   # before the ballot: refused

    conn = connect(db_path)
    conn.execute("UPDATE formals SET status='allocated'")
    set_setting(conn, "results_published", "0")  # ballot run, not yet published
    conn.commit(); conn.close()
    c.post("/formals/1/subscribe", data={"_csrf": "tok"})
    assert _subs(db_path) == 0                   # draft results: refused

    conn = connect(db_path)
    set_setting(conn, "results_published", "1")
    conn.commit(); conn.close()
    c.post("/formals/1/subscribe", data={"_csrf": "tok"})
    assert _subs(db_path) == 1                   # allocated + published: accepted
