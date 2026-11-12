import contextlib
import importlib
import io

from conftest import add_formal, add_user
from swaps.db import connect, set_setting


def _setup(db_path, monkeypatch, published="1"):
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    c = connect(db_path)
    add_user(c, 1); add_user(c, 2)
    add_formal(c, 1, slots=5, dt="2035-01-01 19:30")
    c.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (1,1,'active')")
    c.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (2,1,'cancelled')")
    set_setting(c, "current_term", "T1"); set_setting(c, "results_published", published)
    f = dict(c.execute("SELECT * FROM formals WHERE id=1").fetchone())
    c.close()
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    adm = app.test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    return adm, f


def _edit(adm, f, **changes):
    data = {"_csrf": "tok", "host_college": f["host_college"], "dt": f["dt"],
            "price": f["price"], "slots": str(f["slots"]), "term": f["term"],
            "ballot_open": f["ballot_open"], "ballot_close": f["ballot_close"],
            "status": f["status"]}
    data.update(changes)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = adm.post("/admin/formals/1/edit", data=data, follow_redirects=True)
    return r.data.decode(), buf.getvalue()


def _seq(db_path):
    c = connect(db_path)
    n = c.execute("SELECT ics_seq FROM formals WHERE id=1").fetchone()[0]
    c.close()
    return n


def test_date_change_emails_attendees_with_updated_invite(db_path, monkeypatch):
    adm, f = _setup(db_path, monkeypatch)
    assert "email all attendees with an updated calendar invite" in \
        adm.get("/admin/formals/1/edit").data.decode()
    page, mail = _edit(adm, f, dt="2035-01-08T19:00", notify_date_change="1")
    assert "Emailed 1 attendee(s)" in page
    assert "DEV EMAIL to=u1@pem.cam.ac.uk" in mail and "u2@" not in mail  # active only
    assert "01/01/2035 19:30" in mail and "08/01/2035 19:00" in mail
    assert "College1-formal.ics" in mail
    assert _seq(db_path) == 1
    from swaps.ics import formal_ics
    c = connect(db_path)
    ics = formal_ics(c.execute("SELECT * FROM formals WHERE id=1").fetchone())
    assert "SEQUENCE:1" in ics and "UID:formal-1@" in ics


def test_date_change_unticked_sends_nothing_but_bumps_sequence(db_path, monkeypatch):
    adm, f = _setup(db_path, monkeypatch)
    page, mail = _edit(adm, f, dt="2035-01-08T19:00")
    assert "DEV EMAIL" not in mail and _seq(db_path) == 1


def test_other_edits_send_nothing(db_path, monkeypatch):
    adm, f = _setup(db_path, monkeypatch)
    page, mail = _edit(adm, f, price="£20", notify_date_change="1")
    assert "DEV EMAIL" not in mail and _seq(db_path) == 0


def test_date_change_while_draft_not_emailed(db_path, monkeypatch):
    adm, f = _setup(db_path, monkeypatch, published="0")
    page, mail = _edit(adm, f, dt="2035-01-08T19:00", notify_date_change="1")
    assert "still a draft" in page and "DEV EMAIL" not in mail
