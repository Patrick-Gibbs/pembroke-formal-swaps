import importlib

import pytest

from conftest import add_formal, add_user
from swaps.db import connect
from swaps.services import (ClaimError, MANUAL_HOLD, cancel_allocation, claim_seat,
                            free_seats, open_due_releases, pending_holds,
                            release_mode, release_now, set_release_mode, utcnow_str)


def _setup(db):
    add_user(db, 1); add_user(db, 2); add_user(db, 3)
    add_formal(db, 1, slots=1, dt="2035-01-01 19:30")
    return db.execute("INSERT INTO allocations(user_id, formal_id, status) "
                      "VALUES (1,1,'active')").lastrowid


def test_manual_mode_holds_cancelled_place(db):
    aid = _setup(db)
    assert release_mode(db) == "auto"
    set_release_mode(db, "manual")
    assert release_mode(db) == "manual"
    cancel_allocation(db, 1, aid)
    r = db.execute("SELECT release_at, general_at FROM released_slots").fetchone()
    assert r["release_at"] == MANUAL_HOLD and r["general_at"] == MANUAL_HOLD
    # Never opens on its own, nobody notified, nobody can claim it.
    sent = []
    assert open_due_releases(db, lambda f, e: sent.append(e)) == 0
    assert free_seats(db, 1, 1, priority=True) == 0
    with pytest.raises(ClaimError):
        claim_seat(db, 2, 1)
    # Admin can assign it directly ...
    claim_seat(db, 2, 1, actor="admin", override_holds=True)
    assert pending_holds(db, 1, priority=True) == 0


def test_manual_place_released_by_admin(db):
    aid = _setup(db)
    set_release_mode(db, "manual")
    cancel_allocation(db, 1, aid)
    assert release_now(db, 1, everyone=True) == 1
    claim_seat(db, 3, 1)


def test_switching_modes_holds_and_reschedules(db):
    aid = _setup(db)
    cancel_allocation(db, 1, aid)        # auto: scheduled in the future (or now)
    db.execute("UPDATE released_slots SET release_at='2099-01-01 00:00:00', "
               "general_at='2099-01-01 03:00:00'")
    assert set_release_mode(db, "manual") == 1
    r = db.execute("SELECT release_at, general_at FROM released_slots").fetchone()
    assert r["release_at"] == MANUAL_HOLD and r["general_at"] == MANUAL_HOLD
    assert set_release_mode(db, "auto") == 1
    r = db.execute("SELECT release_at, general_at FROM released_slots").fetchone()
    assert r["release_at"] < MANUAL_HOLD and r["general_at"] < MANUAL_HOLD
    assert r["release_at"] <= r["general_at"]


def test_manual_keeps_priority_open_but_holds_general(db):
    aid = _setup(db)
    cancel_allocation(db, 1, aid)
    db.execute("UPDATE released_slots SET release_at='2000-01-01 00:00:00', "
               "general_at='2099-01-01 00:00:00'")
    assert set_release_mode(db, "manual") == 1
    r = db.execute("SELECT release_at, general_at FROM released_slots").fetchone()
    assert r["release_at"] == "2000-01-01 00:00:00" and r["general_at"] == MANUAL_HOLD
    set_release_mode(db, "auto")
    r = db.execute("SELECT release_at, general_at FROM released_slots").fetchone()
    assert r["release_at"] == "2000-01-01 00:00:00"
    assert utcnow_str() <= r["general_at"] < MANUAL_HOLD


def test_admin_toggle_route(db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    adm = app.test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    assert "Switch to manual release" in adm.get("/admin/").data.decode()
    adm.post("/admin/release-mode", data={"_csrf": "tok", "mode": "manual"})
    c = connect(db_path)
    assert release_mode(c) == "manual"
    c.close()
    assert "Switch to auto release" in adm.get("/admin/").data.decode()
    adm.post("/admin/release-mode", data={"_csrf": "tok", "mode": "auto"})
    c = connect(db_path)
    assert release_mode(c) == "auto"
