import contextlib
import importlib
import io
from datetime import datetime, timedelta

from conftest import add_formal, add_user
from swaps.db import connect, set_setting
from swaps.services import (MANUAL_HOLD, cancel_allocation, claim_seat, free_seats,
                            open_due_releases, pending_holds, release_mode, release_now,
                            set_release_mode)


def _held(db, fid, n):
    """Formal `fid` with n cancelled places (users fid*10+1..)."""
    for i in range(1, n + 1):
        uid = fid * 10 + i
        add_user(db, uid)
        claim_seat(db, uid, fid)
        cancel_allocation(db, uid, db.execute(
            "SELECT id FROM allocations WHERE user_id=? AND formal_id=?",
            (uid, fid)).fetchone()["id"])


def test_per_formal_mode_overrides_default(db):
    add_formal(db, 1, slots=3, dt="2035-01-01 19:30")
    add_formal(db, 2, slots=3, dt="2035-02-01 19:30")
    set_release_mode(db, "manual", formal_id=1)       # default stays auto
    assert release_mode(db, 1) == "manual" and release_mode(db, 2) == "auto"
    _held(db, 1, 1); _held(db, 2, 1)
    rel = dict(db.execute("SELECT formal_id, release_at FROM released_slots").fetchall())
    assert rel[1] == MANUAL_HOLD and rel[2] != MANUAL_HOLD
    # Global switch leaves formals with their own setting alone.
    set_release_mode(db, "auto")
    assert release_mode(db, 1) == "manual"
    set_release_mode(db, "manual")
    assert release_mode(db, 2) == "manual"
    # Back to default: follows the global (now manual).
    set_release_mode(db, "", formal_id=1)
    assert release_mode(db, 1) == "manual"
    set_release_mode(db, "auto", formal_id=1)        # reschedules its held place
    assert db.execute("SELECT release_at FROM released_slots WHERE formal_id=1"
                      ).fetchone()[0] != MANUAL_HOLD


def test_release_one_at_a_time_or_all(db):
    add_formal(db, 1, slots=3, dt="2035-01-01 19:30")
    set_release_mode(db, "manual", formal_id=1)
    _held(db, 1, 3)
    assert pending_holds(db, 1) == 3
    assert release_now(db, 1, everyone=True, limit=1) == 1
    assert pending_holds(db, 1) == 2
    assert release_now(db, 1, everyone=False, limit=1) == 1     # one to no-swaps
    assert pending_holds(db, 1, priority=True) == 1
    assert release_now(db, 1, everyone=True) == 2               # the rest
    assert pending_holds(db, 1) == 0


def test_no_release_after_formal_but_guest_list_editable(db):
    add_user(db, 1); add_user(db, 2); add_user(db, 3)
    soon = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    add_formal(db, 1, slots=2, dt=soon)
    claim_seat(db, 1, 1); claim_seat(db, 2, 1)
    cancel_allocation(db, 1, db.execute("SELECT id FROM allocations WHERE user_id=1"
                                        ).fetchone()["id"])
    db.execute("INSERT INTO subscriptions(user_id, formal_id) VALUES (3, 1)")
    past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    db.execute("UPDATE formals SET dt=?", (past,))
    db.execute("UPDATE released_slots SET release_at='2000-01-01 00:00:00', "
               "general_at='2000-01-01 00:00:00'")
    sent = []
    open_due_releases(db, lambda f, e: sent.append(e))
    assert sent == []                                   # worker never announces
    db.execute("UPDATE released_slots SET release_at=?, general_at=?", (MANUAL_HOLD, MANUAL_HOLD))
    assert release_now(db, 1, everyone=True) == 0       # admin can't release
    # Admin removal after the formal creates nothing to release ...
    n = db.execute("SELECT COUNT(*) FROM released_slots").fetchone()[0]
    cancel_allocation(db, None, db.execute("SELECT id FROM allocations WHERE user_id=2 "
                                           "AND status='active'").fetchone()["id"])
    assert db.execute("SELECT COUNT(*) FROM released_slots").fetchone()[0] == n
    # ... and admin can still add people (billing).
    claim_seat(db, 3, 1, actor="admin", override_holds=True)
    assert free_seats(db, 1, 2) >= 0


def test_admin_routes(db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    c = connect(db_path)
    set_setting(c, "current_term", "T1"); set_setting(c, "results_published", "1")
    add_formal(c, 1, slots=3, dt="2035-01-01 19:30")
    set_release_mode(c, "manual", formal_id=1)
    _held(c, 1, 2)
    past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    add_formal(c, 2, slots=3, dt=past)
    add_user(c, 99)
    c.close()
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    adm = app.test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    page = adm.get("/admin/formals/1/roster").data.decode()
    assert "Release 1 to all" in page and "Release all to all (2)" in page
    assert '<option value="manual" selected' in page
    dash = adm.get("/admin/").data.decode()
    assert "Release 1 to no-swaps" in dash and "Default release mode" in dash
    adm.post("/admin/formals/1/release", data={"_csrf": "tok", "to": "all", "count": "1",
                                               "email": "1"})
    c = connect(db_path)
    assert pending_holds(c, 1) == 1
    c.close()
    adm.post("/admin/formals/1/release-mode", data={"_csrf": "tok", "mode": "auto"})
    c = connect(db_path)
    assert release_mode(c, 1) == "auto"
    c.close()
    # Past formal: admin can add, no confirmation email.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = adm.post("/admin/formals/2/add-user", data={"_csrf": "tok", "user_id": "99"},
                     follow_redirects=True)
    assert "no confirmation email" in r.data.decode() and "DEV EMAIL" not in buf.getvalue()


def test_reminder_says_who_they_are_coming_as(db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", db_path)
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import release_worker
    importlib.reload(release_worker)
    c = connect(db_path)
    add_user(c, 1); add_user(c, 2); add_user(c, 3)
    add_formal(c, 1, slots=2, dt="2035-01-01 19:30")
    c.execute("UPDATE users SET dietary_flags='Vegan' WHERE id=1")
    claim_seat(c, 1, 1); claim_seat(c, 3, 1)
    c.execute("UPDATE formals SET catering_sent=1")
    cancel_allocation(c, 1, c.execute("SELECT id FROM allocations WHERE user_id=1"
                                      ).fetchone()["id"])
    release_now(c, 1, everyone=True)
    claim_seat(c, 2, 1)
    f = c.execute("SELECT * FROM formals WHERE id=1").fetchone()
    c.close()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        release_worker._send_reminders(f, ["u2@pem.cam.ac.uk", "u3@pem.cam.ac.uk"])
    out = buf.getvalue()
    u2 = out[out.index("to=u2@"):out.index("to=u3@")]
    u3 = out[out.index("to=u3@"):]
    assert "on their list as <b>U1 Test</b>, with their meal: <b>Vegan</b>" in u2
    assert "on their list as" not in u3
