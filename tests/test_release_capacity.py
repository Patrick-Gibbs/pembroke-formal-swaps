import importlib

import pytest

from conftest import add_formal, add_user
from swaps.db import connect
from swaps.services import (ClaimError, MANUAL_HOLD, cancel_allocation, claim_seat,
                            drop_surplus_places, open_due_releases, release_now,
                            set_release_mode)


def _cancel(db, uid, fid=1):
    cancel_allocation(db, uid, db.execute(
        "SELECT id FROM allocations WHERE user_id=? AND formal_id=? AND status='active'",
        (uid, fid)).fetchone()["id"])


def _trinity(db):
    """3-slot formal, all taken, manual release; subscriber 9 waiting."""
    for uid in (1, 2, 3, 9):
        add_user(db, uid)
    add_formal(db, 1, slots=3, dt="2035-01-01 19:30")
    set_release_mode(db, "manual", formal_id=1)
    for uid in (1, 2, 3):
        claim_seat(db, uid, 1)
    db.execute("INSERT INTO subscriptions(user_id, formal_id) VALUES (9, 1)")


def _waiting(db):
    return [r[0] for r in db.execute("SELECT orig_name FROM released_slots "
                                     "WHERE claimed_by IS NULL ORDER BY id")]


def test_reducing_slots_drops_surplus_and_release_emails_nobody(db):
    _trinity(db)
    _cancel(db, 1)                                  # place waiting, held
    db.execute("UPDATE formals SET slots=2")        # host cut a place
    _cancel(db, 2)                                  # another cancel: 1 active, 2 slots
    assert release_now(db, 1, everyone=True) == 1   # only one real seat to release
    assert len(_waiting(db)) == 1
    sent = []
    open_due_releases(db, lambda f, e: sent.append(e))
    assert sent == [["u9@pem.cam.ac.uk"]]           # one announcement, claimable
    claim_seat(db, 9, 1)                            # and it can really be claimed


def test_no_announcement_when_formal_already_full(db):
    _trinity(db)
    _cancel(db, 1)
    db.execute("UPDATE formals SET slots=2")        # now 2 active in 2 slots
    db.execute("UPDATE released_slots SET release_at='2000-01-01 00:00:00', "
               "general_at='2000-01-01 00:00:00'")
    sent = []
    open_due_releases(db, lambda f, e: sent.append(e))
    assert sent == [] and _waiting(db) == []        # dropped, not announced
    assert release_now(db, 1, everyone=True) == 0


def test_drop_prefers_unreleased_newest(db):
    _trinity(db)
    _cancel(db, 1); _cancel(db, 2)
    db.execute("UPDATE released_slots SET release_at='2000-01-01 00:00:00' "
               "WHERE orig_name='U1 Test'")         # U1's place already open
    db.execute("UPDATE formals SET slots=2")        # room for 1 waiting place
    assert drop_surplus_places(db, 1) == ["U2 Test"]
    assert _waiting(db) == ["U1 Test"]


def test_admin_add_fills_chosen_or_newest_place(db):
    _trinity(db)
    db.execute("UPDATE formals SET catering_sent=1")
    db.execute("UPDATE users SET dietary_flags='Vegan' WHERE id=1")
    db.execute("UPDATE users SET dietary_flags='Halal' WHERE id=2")
    _cancel(db, 1); _cancel(db, 2)                  # U2 cancelled most recently
    add_user(db, 7); add_user(db, 8)
    # Default: the most recently cancelled place (cancel-then-replace).
    assert claim_seat(db, 7, 1, actor="admin", override_holds=True)["replaced"] == "U2 Test"
    # Explicit choice.
    sid = db.execute("SELECT id FROM released_slots WHERE orig_name='U1 Test'").fetchone()[0]
    assert claim_seat(db, 8, 1, actor="admin", override_holds=True,
                      place=sid) == {"replaced": "U1 Test", "dietary": "Vegan"}


def test_admin_add_free_seat_option(db):
    _trinity(db)
    db.execute("UPDATE formals SET slots=4")        # one seat that was never anyone's
    _cancel(db, 1)
    add_user(db, 7); add_user(db, 8)
    assert claim_seat(db, 7, 1, actor="admin", override_holds=True, place="free") is None
    assert _waiting(db) == ["U1 Test"]              # the cancelled place is untouched
    with pytest.raises(ClaimError, match="no free seat"):
        claim_seat(db, 8, 1, actor="admin", override_holds=True, place="free")


def test_roster_place_menu_and_edit_drops_surplus(db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    c = connect(db_path)
    _trinity(c)
    _cancel(c, 1); _cancel(c, 2)
    f = dict(c.execute("SELECT * FROM formals WHERE id=1").fetchone())
    c.close()
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    adm = app.test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    page = adm.get("/admin/formals/1/roster").data.decode()
    assert "Place to fill" in page
    assert page.index("U2 Test&#39;s place") < page.index("U1 Test&#39;s place")  # newest first
    r = adm.post("/admin/formals/1/edit", data={
        "_csrf": "tok", "host_college": f["host_college"], "dt": f["dt"], "price": "",
        "slots": "2", "term": f["term"], "ballot_open": f["ballot_open"],
        "ballot_close": f["ballot_close"], "status": f["status"]}, follow_redirects=True)
    assert "no room for 1 waiting cancelled place(s)" in r.data.decode()
    c = connect(db_path)
    assert _waiting(c) == ["U1 Test"]               # newest (U2) dropped, held


def test_admin_choice_survives_surplus_drop(db):
    """Two cancelled places but only one seat (slots were cut): choosing the
    older place keeps it and drops the other."""
    _trinity(db)
    _cancel(db, 1); _cancel(db, 2)
    db.execute("UPDATE formals SET slots=2")        # 1 active, room for 1
    add_user(db, 7)
    sid = db.execute("SELECT id FROM released_slots WHERE orig_name='U1 Test'").fetchone()[0]
    db.execute("UPDATE formals SET catering_sent=1")
    assert claim_seat(db, 7, 1, actor="admin", override_holds=True,
                      place=sid)["replaced"] == "U1 Test"
    assert _waiting(db) == []
