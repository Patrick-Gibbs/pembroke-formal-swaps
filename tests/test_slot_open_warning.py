import contextlib
import importlib
import io

from conftest import add_formal, add_user
from swaps.db import connect
from swaps.services import cancel_allocation, claim_seat, open_due_releases, release_now


def _run(db_path, monkeypatch, catering_sent, meals):
    monkeypatch.setenv("DB_PATH", db_path)
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import release_worker
    importlib.reload(release_worker)
    c = connect(db_path)
    add_formal(c, 1, slots=len(meals), dt="2035-01-01 19:30")
    add_user(c, 99)
    c.execute("INSERT INTO subscriptions(user_id, formal_id) VALUES (99, 1)")
    for uid, meal in enumerate(meals, 1):
        add_user(c, uid)
        c.execute("UPDATE users SET dietary_flags=? WHERE id=?", (meal, uid))
        claim_seat(c, uid, 1)
    c.execute("UPDATE formals SET catering_sent=?", (catering_sent,))
    for uid in range(1, len(meals) + 1):
        cancel_allocation(c, uid, c.execute(
            "SELECT id FROM allocations WHERE user_id=?", (uid,)).fetchone()[0])
    release_now(c, 1, everyone=True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        open_due_releases(c, release_worker._notify)
    return buf.getvalue()


def test_slot_open_email_warns_and_names_meal_after_list_sent(db_path, monkeypatch):
    out = _run(db_path, monkeypatch, 1, ["Vegan"])
    assert "already has the final catering list" in out
    assert "meal already ordered for it: <b>Vegan</b>" in out


def test_slot_open_email_warns_without_meal_when_places_differ(db_path, monkeypatch):
    out = _run(db_path, monkeypatch, 1, ["Vegan", "Halal"])
    assert "meal already ordered for it. Only claim" in out


def test_slot_open_email_no_warning_before_list_sent(db_path, monkeypatch):
    out = _run(db_path, monkeypatch, 0, ["Vegan"])
    assert "A place has opened" in out and "final catering list" not in out
