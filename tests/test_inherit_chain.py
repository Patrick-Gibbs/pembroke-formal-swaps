import contextlib
import io

from conftest import add_formal, add_user
from swaps.db import connect, init_db
from swaps.services import (cancel_allocation, catering_list, claim_seat,
                            delete_user_data, release_now)


def _chain(db):
    """A (1, Vegan) is on the host list; B (2) takes the seat, then C (3)."""
    for uid in (1, 2, 3):
        add_user(db, uid)
    add_formal(db, 1, slots=1, dt="2035-01-01 19:30")
    db.execute("UPDATE users SET dietary_flags='Vegan' WHERE id=1")
    db.execute("UPDATE users SET dietary_flags='Halal' WHERE id=2")
    db.execute("UPDATE users SET dietary_flags='Kosher' WHERE id=3")
    claim_seat(db, 1, 1)
    db.execute("UPDATE formals SET catering_sent=1")
    for leaver, claimer in ((1, 2), (2, 3)):
        cancel_allocation(db, leaver, db.execute(
            "SELECT id FROM allocations WHERE user_id=? AND status='active'",
            (leaver,)).fetchone()["id"])
        release_now(db, 1, everyone=True)
        info = claim_seat(db, claimer, 1)
        assert info == {"replaced": "U1 Test", "dietary": "Vegan"}


def test_second_takeover_still_names_original_person(db):
    _chain(db)
    c = db.execute("SELECT inherited_from, inherited_dietary FROM allocations "
                   "WHERE user_id=3 AND status='active'").fetchone()
    assert c["inherited_from"] == "U1 Test" and c["inherited_dietary"] == "Vegan"
    rows = catering_list(db, 1)
    assert [(r["first_name"], r["replaces"], r["dietary"]) for r in rows] == \
        [("U3", "U1 Test", "Vegan")]
    from swaps import emailer
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        emailer.catering_email(["h@x.ac.uk"], None, {"host_college": "X", "dt": "d",
                               "host_name": "", "host_email": "h@x.ac.uk"}, rows, "")
    assert "U3 Test <i>(in place of U1 Test)</i>" in buf.getvalue()


def test_erasing_original_person_scrubs_name_down_the_chain(db):
    _chain(db)
    delete_user_data(db, 1)
    assert db.execute("SELECT inherited_from FROM allocations WHERE user_id=3 "
                      "AND status='active'").fetchone()[0] == "a member who cancelled"
    assert "U1 Test" not in [r[0] for r in db.execute(
        "SELECT orig_name FROM released_slots")]


def test_repair_existing_chains(tmp_path):
    """Data written by the old code (each link named the previous claimer) is
    repaired once on startup."""
    path = str(tmp_path / "t.db")
    init_db(path)
    db = connect(path)
    _chain(db)
    # Rewrite as the old code would have left it.
    b_alloc = db.execute("SELECT id FROM allocations WHERE user_id=2").fetchone()[0]
    db.execute("UPDATE released_slots SET orig_name='U2 Test', orig_alloc=NULL "
               "WHERE allocation_id=?", (b_alloc,))
    db.execute("UPDATE released_slots SET orig_alloc=NULL")
    db.execute("UPDATE allocations SET inherited_from='U2 Test' WHERE user_id=3")
    db.execute("ALTER TABLE allocations DROP COLUMN inherited_from_alloc")
    db.close()
    init_db(path)
    db = connect(path)
    assert db.execute("SELECT inherited_from FROM allocations WHERE user_id=3 "
                      "AND status='active'").fetchone()[0] == "U1 Test"
    assert [r[0] for r in db.execute("SELECT orig_name FROM released_slots ORDER BY id")] \
        == ["U1 Test", "U1 Test"]
