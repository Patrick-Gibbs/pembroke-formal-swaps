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
    adm = app.test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    return adm


def _add(adm, fid, uid):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = adm.post(f"/admin/formals/{fid}/add-user",
                     data={"_csrf": "tok", "user_id": str(uid)}, follow_redirects=True)
    return r.data.decode(), buf.getvalue()


def _notified(db_path, uid):
    c = connect(db_path)
    n = c.execute("SELECT notified FROM allocations WHERE user_id=? AND status='active'",
                  (uid,)).fetchone()[0]
    c.close()
    return n


def test_admin_assign_sends_confirmation_with_ics(db_path, monkeypatch):
    c = connect(db_path)
    add_user(c, 1); add_formal(c, 1, slots=3, dt="2035-01-01 19:30")
    c.execute("UPDATE formals SET wine_fee=1, wine_price='£6'")
    set_setting(c, "current_term", "T1"); set_setting(c, "results_published", "1")
    c.close()
    page, mail = _add(_app(db_path, monkeypatch), 1, 1)
    assert "emailed a confirmation" in page
    assert "DEV EMAIL to=u1@pem.cam.ac.uk" in mail and "College1-formal.ics" in mail
    assert "You&#39;re in" in page or "You're in" in mail
    assert "Wine is an extra fee (£6)" in mail
    assert _notified(db_path, 1) == 1          # publish won't email it again


def test_admin_assign_waits_for_publish_while_draft(db_path, monkeypatch):
    c = connect(db_path)
    add_user(c, 1); add_formal(c, 1, slots=3, dt="2035-01-01 19:30")
    set_setting(c, "current_term", "T1"); set_setting(c, "results_published", "0")
    c.close()
    page, mail = _add(_app(db_path, monkeypatch), 1, 1)
    assert "when you publish" in page
    assert "DEV EMAIL" not in mail
    assert _notified(db_path, 1) == 0          # still picked up by publish


def test_admin_assign_after_list_sent_mentions_fixed_meal(db_path, monkeypatch):
    from swaps.services import cancel_allocation, claim_seat
    c = connect(db_path)
    add_user(c, 1); add_user(c, 2); add_formal(c, 1, slots=1, dt="2035-01-01 19:30")
    set_setting(c, "current_term", "T1"); set_setting(c, "results_published", "1")
    c.execute("UPDATE users SET dietary_flags='Vegan' WHERE id=1")
    claim_seat(c, 1, 1)
    c.execute("UPDATE formals SET catering_sent=1")
    cancel_allocation(c, 1, c.execute("SELECT id FROM allocations").fetchone()[0])
    c.close()
    page, mail = _add(_app(db_path, monkeypatch), 1, 2)
    assert "taking the place of <b>U1 Test</b>" in mail and "Vegan" in mail
    assert "College1-formal.ics" in mail
