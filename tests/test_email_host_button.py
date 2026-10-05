import importlib

from conftest import add_formal, add_user
from swaps.db import connect, set_setting


def test_email_host_from_dashboard(db_path, monkeypatch):
    c = connect(db_path)
    add_user(c, 1)
    add_formal(c, 1, slots=5)                       # allocated
    c.execute("UPDATE formals SET host_email='catering@college.cam.ac.uk' WHERE id=1")
    c.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (1,1,'active')")
    set_setting(c, "current_term", "T1")
    c.commit(); c.close()
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    a = app.test_client()
    with a.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    assert b"Email host" in a.get("/admin/").data
    r = a.post("/admin/formals/1/catering-email", data={"_csrf": "tok", "back": "dashboard"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/")
    c = connect(db_path)
    assert c.execute("SELECT catering_sent FROM formals WHERE id=1").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM email_log WHERE recipient LIKE "
                     "'catering@college.cam.ac.uk%'").fetchone()[0] == 1
    c.close()
    assert "List sent".encode() in a.get("/admin/").data
