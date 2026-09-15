import os

from conftest import add_formal, add_user
from swaps import chatbot
from swaps.db import set_setting
from swaps.security import (CHAT_GLOBAL_PER_DAY, CHAT_GLOBAL_PER_WEEK,
                            CHAT_LIMIT_PER_HOUR, chat_global_limited,
                            chat_rate_limited)


def _seed_term(db):
    set_setting(db, "current_term", "T1")
    add_formal(db, 1, slots=10, term="T1")
    db.execute("UPDATE formals SET status='allocated', host_college='Trinity Hall' "
               "WHERE id=1")


def test_context_lists_formal(db):
    _seed_term(db)
    ctx = chatbot.build_context(db)
    assert "Trinity Hall" in ctx
    assert "Current term: T1" in ctx


def test_context_hides_attendees_until_published(db):
    _seed_term(db)
    add_user(db, 1)
    db.execute("INSERT INTO allocations(user_id, formal_id, status) "
               "VALUES (1, 1, 'active')")
    set_setting(db, "attendee_list_public", "1")
    set_setting(db, "results_published", "0")
    assert "U1 Test" not in chatbot.build_context(db)  # add_user names are U<id> Test
    set_setting(db, "results_published", "1")
    assert "U1 Test" in chatbot.build_context(db)


def test_context_hides_attendees_when_list_private(db):
    _seed_term(db)
    add_user(db, 2)
    db.execute("INSERT INTO allocations(user_id, formal_id, status) "
               "VALUES (2, 1, 'active')")
    set_setting(db, "results_published", "1")
    set_setting(db, "attendee_list_public", "0")
    assert "U2 Test" not in chatbot.build_context(db)


def test_per_ip_rate_limit(db):
    ip = "1.2.3.4"
    for _ in range(CHAT_LIMIT_PER_HOUR):
        assert chat_rate_limited(db, ip) is False
    assert chat_rate_limited(db, ip) is True          # (N+1)th blocked
    assert chat_rate_limited(db, "9.9.9.9") is False  # other IP unaffected


def test_global_daily_cap(db):
    for i in range(CHAT_GLOBAL_PER_DAY):
        assert chat_global_limited(db) is False
        chat_rate_limited(db, f"10.0.0.{i}")          # log one request each
    assert chat_global_limited(db) is True


def test_chat_endpoint(db_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    import importlib
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import chatbot as cb
    importlib.reload(cb)
    monkeypatch.setattr(cb, "answer", lambda db, msg, hist: f"echo:{msg}")

    from swaps import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with client.session_transaction() as s:
        s["_csrf"] = "tok"

    # missing/invalid CSRF -> 400
    r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 400

    r = client.post("/chat", json={"_csrf": "tok", "message": "When is the formal?"})
    assert r.status_code == 200 and r.get_json()["reply"] == "echo:When is the formal?"

    # empty message -> 400
    r = client.post("/chat", json={"_csrf": "tok", "message": "   "})
    assert r.status_code == 400
