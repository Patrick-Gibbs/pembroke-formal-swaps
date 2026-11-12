import contextlib
import importlib
import io

from conftest import add_formal, add_user
from swaps.db import connect, set_setting


def _app(db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    return app


def test_page_views_recorded_and_usage_page(db_path, monkeypatch):
    c = connect(db_path)
    add_user(c, 1); add_formal(c, 1, slots=3, dt="2035-01-01 19:30")
    c.execute("INSERT INTO incoming_swaps(guest_college, dt, tickets, token) "
              "VALUES ('X', '2035-01-01 19:30', 2, 'secrettoken123')")
    c.close()
    app = _app(db_path, monkeypatch)
    member = app.test_client()
    with member.session_transaction() as s:
        s["uid"] = 1
    member.get("/"); member.get("/me"); member.get("/formals/1/claim")
    anon = app.test_client()
    anon.get("/", headers={"User-Agent": "Mozilla/5.0 (iPhone)"})
    anon.get("/guests/secrettoken123")
    app.test_client().get("/", headers={"User-Agent": "Googlebot/2.1"})
    app.test_client().get("/sw.js")
    c = connect(db_path)
    rows = c.execute("SELECT route, user_id, formal_id, mobile, bot FROM page_views").fetchall()
    routes = [r["route"] for r in rows]
    assert "/sw.js" not in routes
    assert "/guests/<token>" in routes and not any("secrettoken" in r for r in routes)
    assert sum(1 for r in rows if r["user_id"] == 1) == 3
    assert any(r["formal_id"] == 1 for r in rows)
    assert sum(r["bot"] for r in rows) == 1 and sum(r["mobile"] for r in rows) == 1
    # A login is counted from login_attempts.
    c.execute("INSERT INTO login_attempts(kind, identifier, success) "
              "VALUES ('user', 'u1@pem.cam.ac.uk', 1)")
    c.close()
    adm = app.test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    page = adm.get("/admin/usage?range=7").data.decode()
    assert "Site usage" in page and "Page views per day" in page
    assert "<b>5</b><br><span class=\"muted\">page views" in page      # bots + admin excluded
    assert "/formals/&lt;int:formal_id&gt;/claim" in page
    assert "U1 Test" in page
    for r in ("30", "90", "all"):
        assert adm.get(f"/admin/usage?range={r}").status_code == 200
    assert "Site usage" in adm.get("/admin/").data.decode()


def test_erasure_removes_page_views(db):
    from swaps.services import delete_user_data
    add_user(db, 1)
    db.execute("INSERT INTO page_views(user_id, method, route, status) VALUES (1,'GET','/',200)")
    delete_user_data(db, 1)
    assert db.execute("SELECT COUNT(*) FROM page_views").fetchone()[0] == 0


def test_admin_can_edit_and_submit_guest_list(db_path, monkeypatch):
    c = connect(db_path)
    set_setting(c, "catering_email", "fh@pem.cam.ac.uk")
    set_setting(c, "admin_email", "me@pem.cam.ac.uk")
    sid = c.execute("INSERT INTO incoming_swaps(guest_college, dt, tickets, host_email) "
                    "VALUES ('Kings', '2035-06-20 19:30', 3, 'k@kings.cam.ac.uk')").lastrowid
    c.close()
    adm = _app(db_path, monkeypatch).test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    assert adm.get(f"/admin/incoming/{sid}/guests").data.decode().count('name="first_') == 3
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = adm.post(f"/admin/incoming/{sid}/guests", data={
            "_csrf": "tok", "action": "save", "first_0": "Ann", "last_0": "Lee"},
            follow_redirects=True)
    assert "Saved 1 guest(s)." in r.data.decode() and "DEV EMAIL" not in buf.getvalue()
    c = connect(db_path)
    assert c.execute("SELECT submitted_at FROM incoming_swaps").fetchone()[0] is None
    with contextlib.redirect_stdout(buf):
        r = adm.post(f"/admin/incoming/{sid}/guests", data={
            "_csrf": "tok", "action": "submit", "first_0": "Ann", "last_0": "Lee",
            "first_1": "Bo", "last_1": "Ng", "dietary_1": "Halal"}, follow_redirects=True)
    assert "submitted" in r.data.decode()
    assert "DEV EMAIL to=fh@pem.cam.ac.uk" in buf.getvalue() and "Bo Ng" in buf.getvalue()
    s = c.execute("SELECT * FROM incoming_swaps").fetchone()
    assert s["submitted_at"] and s["catering_sent_at"]
    # Admin can still correct it after submission (the contact can't).
    adm.post(f"/admin/incoming/{sid}/guests", data={
        "_csrf": "tok", "action": "save", "first_0": "Ann", "last_0": "Lee-Smith"})
    assert c.execute("SELECT last_name FROM incoming_participants ORDER BY id").fetchone()[0] \
        == "Lee-Smith"
    page = adm.get(f"/admin/incoming/{sid}/guests").data.decode()
    assert "Save &amp; resend to Formal Hall team" in page
