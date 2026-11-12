import contextlib
import importlib
import io
import re
from datetime import datetime, timedelta

from conftest import add_formal
from swaps.db import connect, init_db, set_setting


def _app(db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    return app


def _admin(app):
    adm = app.test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    return adm


def _mail(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


def _settings(db_path):
    c = connect(db_path)
    set_setting(c, "admin_email", "me@pem.cam.ac.uk")
    set_setting(c, "admin_name", "Pat")
    set_setting(c, "catering_email", "formalhall@pem.cam.ac.uk")
    set_setting(c, "current_term", "T1")
    c.close()


FORMAL = {"_csrf": "tok", "host_college": "Jesus", "dt": "2035-05-10T19:30",
          "slots": "5", "term": "T1", "status": "allocated", "host_name": "Jo",
          "host_email": "jo@jesus.cam.ac.uk"}


def test_formal_form_sets_up_incoming_and_full_flow(db_path, monkeypatch):
    _settings(db_path)
    app = _app(db_path, monkeypatch)
    adm = _admin(app)
    adm.post("/admin/formals/new", data=dict(FORMAL, incoming_tickets="3",
                                             incoming_dt="2035-06-01T19:30"))
    c = connect(db_path)
    s = c.execute("SELECT * FROM incoming_swaps").fetchone()
    fid = c.execute("SELECT id FROM formals WHERE host_college='Jesus'").fetchone()[0]
    assert (s["formal_id"], s["tickets"], s["dt"], s["guest_college"], s["host_email"]) == \
        (fid, 3, "2035-06-01 19:30", "Jesus", "jo@jesus.cam.ac.uk")
    # Editing the formal keeps the pairing in sync.
    adm.post(f"/admin/formals/{fid}/edit", data=dict(FORMAL, incoming_tickets="4",
                                                     incoming_dt="2035-06-01T19:30",
                                                     host_email="new@jesus.cam.ac.uk"))
    s = c.execute("SELECT * FROM incoming_swaps").fetchone()
    assert s["tickets"] == 4 and s["host_email"] == "new@jesus.cam.ac.uk"
    assert 'name="incoming_tickets"' in adm.get(f"/admin/formals/{fid}/edit").data.decode()

    # Admin emails the link.
    page = adm.get("/admin/incoming").data.decode()
    assert "Email guest-list links to all (1)" in page
    r, mail = _mail(adm.post, "/admin/incoming/send-links", data={"_csrf": "tok"},
                    follow_redirects=True)
    assert "Sent 1 guest-list link(s)" in r.data.decode()
    link = re.search(r"/guests/[A-Za-z0-9_-]+", mail).group(0)
    assert "DEV EMAIL to=new@jesus.cam.ac.uk" in mail and "22/05/2035" in mail  # 10 days

    # The contact fills it in without any account.
    guest = app.test_client()
    form = guest.get(link).data.decode()
    assert form.count('name="first_') == 4
    with guest.session_transaction() as sess:
        sess["_csrf"] = "g"
    rows = {"_csrf": "g", "first_0": "Ann", "last_0": "Lee", "dietary_0": "Vegan",
            "first_1": "Bob", "last_1": "Ray"}
    r = guest.post(link, data=rows)                         # no confirmation tick
    assert "final guest list" in r.data.decode()
    assert c.execute("SELECT submitted_at FROM incoming_swaps").fetchone()[0] is None
    r = guest.post(link, data=dict(rows, first_2="Cy"))     # half a name
    assert "Guest 3" in r.data.decode()
    (r, mail) = _mail(guest.post, link, data=dict(rows, confirm="1"), follow_redirects=True)
    assert "has been submitted" in r.data.decode()
    assert "DEV EMAIL to=formalhall@pem.cam.ac.uk" in mail
    assert "new@jesus.cam.ac.uk" in mail and "me@pem.cam.ac.uk" in mail   # both CC'd
    assert "Dear Formal Hall team" in mail and "Ann Lee" in mail and "Vegan ×1" in mail
    s = c.execute("SELECT * FROM incoming_swaps").fetchone()
    assert s["submitted_at"] and s["catering_sent_at"]
    # Locked: a second submission changes nothing.
    guest.post(link, data={"_csrf": "g", "first_0": "Zed", "last_0": "Q", "confirm": "1"})
    assert [r[0] for r in c.execute("SELECT first_name FROM incoming_participants "
                                    "ORDER BY id")] == ["Ann", "Bob"]
    assert 'name="first_0"' not in guest.get(link).data.decode()
    assert guest.get("/guests/not-a-real-token").status_code == 404


def _swap(db, dt, tickets=2, sent=True):
    sid = db.execute("INSERT INTO incoming_swaps(guest_college, dt, host_name, host_email, "
                     "tickets) VALUES ('Kings', ?, 'Kim', 'kim@kings.cam.ac.uk', ?)",
                     (dt, tickets)).lastrowid
    if sent:
        db.execute("UPDATE incoming_swaps SET link_sent_at='2000-01-01 00:00:00' "
                   "WHERE id=?", (sid,))
    return sid


def test_reminders_three_before_on_and_two_after(db):
    from swaps import incoming as inc
    set_setting(db, "admin_email", "me@pem.cam.ac.uk")
    _swap(db, "2035-06-20 19:30")          # deadline 10/06/2035
    at = lambda d, h=9: datetime(2035, 6, d, h, 0)
    assert inc.send_due_reminders(db, at(6, 23)) == []            # before the window
    (r, mail) = _mail(inc.send_due_reminders, db, at(7))
    assert r == [(1, 1)] and "due in 3 days" in mail and "me@pem.cam.ac.uk" in mail
    assert inc.send_due_reminders(db, at(8)) == []
    assert inc.send_due_reminders(db, at(10, 8)) == []            # before 9am
    (r, mail) = _mail(inc.send_due_reminders, db, at(10))
    assert r == [(1, 2)] and "due <b>today</b>" in mail
    (r, mail) = _mail(inc.send_due_reminders, db, at(12))
    assert r == [(1, 3)] and "was due on" in mail
    assert inc.send_due_reminders(db, at(15)) == []               # then that's it


def test_no_reminders_once_submitted_or_before_link_sent(db):
    from swaps import incoming as inc
    _swap(db, "2035-06-20 19:30", sent=False)
    sid = _swap(db, "2035-06-20 19:30")
    db.execute("UPDATE incoming_swaps SET submitted_at='2035-06-01 00:00:00' WHERE id=?", (sid,))
    assert _mail(inc.send_due_reminders, db, datetime(2035, 6, 12, 9))[0] == []


def test_late_link_skips_reminders_already_due(db_path, monkeypatch):
    from swaps import incoming as inc
    c = connect(db_path)
    soon = (datetime.now() + timedelta(days=8)).strftime("%Y-%m-%d 19:30")
    sid = _swap(c, soon, sent=False)                    # deadline already passed
    ok, _ = _mail(inc.send_link, c, sid)[0]
    assert ok
    assert c.execute("SELECT reminder_stage FROM incoming_swaps").fetchone()[0] == 2
    assert _mail(inc.send_due_reminders, c)[0] == []    # no reminder straight after


def test_standalone_incoming_swap(db_path, monkeypatch):
    _settings(db_path)
    adm = _admin(_app(db_path, monkeypatch))
    adm.post("/admin/incoming/new", data={"_csrf": "tok", "guest_college": "Clare",
                                          "dt": "2035-07-01T19:30", "tickets": "6",
                                          "host_email": "cl@clare.cam.ac.uk"})
    c = connect(db_path)
    s = c.execute("SELECT * FROM incoming_swaps").fetchone()
    assert s["formal_id"] is None and s["tickets"] == 6
    page = adm.get("/admin/incoming").data.decode()
    assert "/guests/" in page and "Email link" in page


def test_deleting_formal_keeps_incoming_standalone(db_path, monkeypatch):
    _settings(db_path)
    adm = _admin(_app(db_path, monkeypatch))
    adm.post("/admin/formals/new", data=dict(FORMAL, incoming_tickets="2",
                                             incoming_dt="2035-06-01T19:30"))
    c = connect(db_path)
    fid = c.execute("SELECT id FROM formals").fetchone()[0]
    adm.post(f"/admin/formals/{fid}/delete", data={"_csrf": "tok"})
    assert c.execute("SELECT formal_id FROM incoming_swaps").fetchone()[0] is None


def test_migration_pairs_existing_incoming_by_college(tmp_path):
    path = str(tmp_path / "t.db")
    init_db(path)
    c = connect(path)
    add_formal(c, 1, slots=5)        # College1
    add_formal(c, 2, slots=5)        # College2
    c.execute("INSERT INTO incoming_swaps(guest_college, dt) VALUES ('college1 ', '2035-01-01 19:30')")
    c.execute("INSERT INTO incoming_swaps(guest_college, dt) VALUES ('Nowhere', '2035-01-01 19:30')")
    c.execute("ALTER TABLE incoming_swaps DROP COLUMN formal_id")
    c.close()
    init_db(path)
    c = connect(path)
    assert [tuple(r) for r in c.execute(
        "SELECT guest_college, formal_id FROM incoming_swaps ORDER BY id")] == \
        [("college1 ", 1), ("Nowhere", None)]
