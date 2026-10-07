import importlib
import io
import contextlib

from conftest import add_formal, add_user
from swaps.db import connect, set_setting


def _app(db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    return app


def test_wine_fee_end_to_end(db_path, monkeypatch):
    c = connect(db_path)
    add_user(c, 1); add_user(c, 2)
    set_setting(c, "current_term", "T1")
    c.commit(); c.close()
    app = _app(db_path, monkeypatch)
    adm = app.test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    adm.post("/admin/formals/new", data={
        "_csrf": "tok", "host_college": "Jesus", "dt": "2035-05-10T19:30", "slots": "5",
        "term": "T1", "status": "allocated", "wine_fee": "on", "wine_price": "£6"})
    c = connect(db_path)
    f = c.execute("SELECT * FROM formals WHERE host_college='Jesus'").fetchone()
    assert f["wine_fee"] == 1 and f["wine_price"] == "£6"
    fid = f["id"]
    a1 = c.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (1,?,'active')",
                   (fid,)).lastrowid
    a2 = c.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (2,?,'active')",
                   (fid,)).lastrowid
    c.commit(); c.close()

    m = app.test_client()
    with m.session_transaction() as s:
        s["uid"] = 1; s["_csrf"] = "tok"
    assert "Opt out" in m.get("/me").data.decode()
    m.post(f"/account/wine/{a1}", data={"_csrf": "tok", "wine": "no"})
    m.post(f"/account/wine/{a2}", data={"_csrf": "tok", "wine": "no"})   # not theirs
    c = connect(db_path)
    assert c.execute("SELECT wine_opt_out FROM allocations WHERE id=?", (a1,)).fetchone()[0] == 1
    assert c.execute("SELECT wine_opt_out FROM allocations WHERE id=?", (a2,)).fetchone()[0] == 0

    from swaps.services import catering_list
    rows = catering_list(c, fid)
    assert sorted(r["wine"] for r in rows) == ["No", "Yes"]
    from swaps import emailer
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        emailer.catering_email(["h@x.ac.uk"], None, f, rows, "")
    out = buf.getvalue()
    assert "Wine</th>" in out and "Wine: 1 yes, 1 no" in out

    # locked once the host has the list
    c.execute("UPDATE formals SET catering_sent=1 WHERE id=?", (fid,)); c.commit(); c.close()
    m.post(f"/account/wine/{a1}", data={"_csrf": "tok", "wine": "yes"})
    c = connect(db_path)
    assert c.execute("SELECT wine_opt_out FROM allocations WHERE id=?", (a1,)).fetchone()[0] == 1
    assert "list sent" in m.get("/me").data.decode()
    assert "Wine" in adm.get(f"/admin/formals/{fid}/roster").data.decode()
    assert "yes" in adm.get(f"/admin/formals/{fid}/roster?csv=1").data.decode()


def test_swap_resets_wine_choice(db):
    from swaps.services import accept_swap, propose_swap
    add_user(db, 1); add_user(db, 2)
    add_formal(db, 1, slots=5, dt="2035-01-01 19:30"); add_formal(db, 2, slots=5, dt="2035-02-01 19:30")
    db.execute("UPDATE formals SET wine_fee=1")
    db.execute("INSERT INTO allocations(user_id, formal_id, status, wine_opt_out) VALUES (1,1,'active',1)")
    db.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (2,2,'active')")
    accept_swap(db, propose_swap(db, 1, 1, 2, 2), 2)
    assert db.execute("SELECT wine_opt_out FROM allocations WHERE user_id=2 AND formal_id=1"
                      ).fetchone()[0] == 0


def test_claim_asks_for_wine_choice(db_path, monkeypatch):
    c = connect(db_path)
    add_user(c, 1); add_user(c, 2); add_user(c, 3)
    add_formal(c, 1, slots=5, dt="2035-05-10 19:30")
    add_formal(c, 2, slots=5, dt="2035-06-10 19:30")
    c.execute("UPDATE formals SET wine_fee=1, wine_price='£6', status='allocated' WHERE id=1")
    c.execute("UPDATE formals SET status='allocated' WHERE id=2")
    c.commit(); c.close()
    app = _app(db_path, monkeypatch)

    def client(uid):
        m = app.test_client()
        with m.session_transaction() as s:
            s["uid"] = uid; s["_csrf"] = "tok"
        return m

    def opt_out(uid, fid):
        c = connect(db_path)
        r = c.execute("SELECT wine_opt_out FROM allocations WHERE user_id=? AND formal_id=? "
                      "AND status='active'", (uid, fid)).fetchone()
        c.close()
        return None if r is None else r[0]

    page = client(1).get("/formals/1/claim").data.decode()
    assert 'name="wine" value="yes"' in page and 'name="wine" value="no"' in page
    assert 'name="wine"' not in client(1).get("/formals/2/claim").data.decode()

    m = client(1)
    m.post("/formals/1/claim", data={"_csrf": "tok"})          # no choice -> not claimed
    assert opt_out(1, 1) is None
    m.post("/formals/1/claim", data={"_csrf": "tok", "wine": "no"})
    assert opt_out(1, 1) == 1
    client(2).post("/formals/1/claim", data={"_csrf": "tok", "wine": "yes"})
    assert opt_out(2, 1) == 0
    client(3).post("/formals/2/claim", data={"_csrf": "tok"})  # no wine fee: no prompt needed
    assert opt_out(3, 2) == 0

    from swaps.services import catering_list
    c = connect(db_path)
    assert sorted(r["wine"] for r in catering_list(c, 1)) == ["No", "Yes"]
    assert [r["wine"] for r in catering_list(c, 2)] == [None]
