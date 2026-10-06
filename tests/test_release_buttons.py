import importlib
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from conftest import add_formal, add_user
from swaps.db import connect, set_setting


class _Confirms(HTMLParser):
    def __init__(self):
        super().__init__(); self.msgs = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if "data-confirm" in d:
            self.msgs.append(d["data-confirm"])


def _msgs(html):
    p = _Confirms(); p.feed(html); return p.msgs


def _utc(s):
    return (datetime.now(timezone.utc) + timedelta(seconds=s)).strftime("%Y-%m-%d %H:%M:%S")


def test_release_buttons_and_apostrophe_safe_dialogs(db_path, monkeypatch):
    c = connect(db_path)
    add_user(c, 1); add_user(c, 2)
    soon = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d %H:%M")
    add_formal(c, 1, slots=2, dt=soon)
    c.execute("UPDATE formals SET host_college=?, term='T1'", ("Queens' College",))
    c.execute("INSERT INTO allocations(id, user_id, formal_id, status) VALUES (9,1,1,'cancelled')")
    c.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (2,1,'active')")
    c.execute("INSERT INTO released_slots(formal_id, allocation_id, release_at, general_at) "
              "VALUES (1, 9, ?, ?)", (_utc(3600), _utc(7200)))
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

    for page in ("/admin/", "/admin/formals/1/roster"):
        html = a.get(page).data.decode()
        assert "Release to no-swaps (1)" in html, page
        assert "Release to all (1)" in html, page
        msgs = _msgs(html)
        assert any("Queens' College now to members with NO swaps" in m for m in msgs), page

    # release to no-swaps: priority opens now, general stays later
    a.post("/admin/formals/1/release", data={"_csrf": "tok", "to": "noswaps"})
    c = connect(db_path)
    r = c.execute("SELECT release_at, general_at FROM released_slots").fetchone()
    now = _utc(5)
    assert r["release_at"] <= now < r["general_at"]
    c.close()
    html = a.get("/admin/").data.decode()
    assert "Release to no-swaps" not in html and "Release to all (1)" in html
    # release to all
    a.post("/admin/formals/1/release", data={"_csrf": "tok", "to": "all"})
    c = connect(db_path)
    assert c.execute("SELECT general_at FROM released_slots").fetchone()[0] <= _utc(5)
    c.close()
    assert "Release to all" not in a.get("/admin/").data.decode()

    # member-side cancel dialog with an apostrophe in the college name
    m = app.test_client()
    with m.session_transaction() as s:
        s["uid"] = 2
    assert any("Queens' College" in x for x in _msgs(m.get("/me").data.decode()))
