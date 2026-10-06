import importlib

from conftest import add_formal
from swaps.db import connect


def _admin(db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    a = app.test_client()
    with a.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    return a


def _form(**kw):
    d = {"_csrf": "tok", "host_college": "Jesus", "dt": "2030-05-10T19:30",
         "slots": "5", "term": "T1", "status": "open"}
    d.update(kw)
    return d


def test_edit_sets_host_email_date(db_path, monkeypatch):
    c = connect(db_path)
    add_formal(c, 1, slots=5, dt="2030-05-10 19:30")
    c.execute("UPDATE formals SET catering_lead_days=3")
    c.commit(); c.close()
    a = _admin(db_path, monkeypatch)
    # legacy lead days shown as the equivalent date
    assert b'value="2030-05-07"' in a.get("/admin/formals/1/edit").data
    r = a.post("/admin/formals/1/edit", data=_form(catering_date="2030-05-01"))
    assert r.status_code == 302
    c = connect(db_path)
    row = c.execute("SELECT catering_at, catering_lead_days FROM formals").fetchone()
    assert tuple(row) == ("2030-05-01 09:00", None)
    c.close()
    assert b'value="2030-05-01"' in a.get("/admin/formals/1/edit").data


def test_host_email_date_must_be_before_formal(db_path, monkeypatch):
    c = connect(db_path)
    add_formal(c, 1, slots=5, dt="2030-05-10 19:30")
    c.commit(); c.close()
    a = _admin(db_path, monkeypatch)
    r = a.post("/admin/formals/1/edit", data=_form(catering_date="2030-05-11"))
    assert r.status_code == 200 and b"must be before the formal" in r.data
    r = a.post("/admin/formals/new", data=_form(catering_date="2030-05-12"))
    assert b"must be before the formal" in r.data
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM formals").fetchone()[0] == 1
    assert c.execute("SELECT catering_at FROM formals").fetchone()[0] is None
    c.close()


def test_duplicate_drops_fixed_host_email_date(db_path, monkeypatch):
    c = connect(db_path)
    add_formal(c, 1, slots=5, dt="2030-05-10 19:30")
    c.execute("UPDATE formals SET catering_at='2030-05-01 09:00'")
    c.commit(); c.close()
    a = _admin(db_path, monkeypatch)
    a.post("/admin/formals/1/duplicate", data={"_csrf": "tok"})
    c = connect(db_path)
    assert c.execute("SELECT catering_at FROM formals WHERE id<>1").fetchone()[0] is None
    c.close()
