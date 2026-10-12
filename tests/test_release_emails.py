import sqlite3

from conftest import add_formal, add_user
from swaps.db import connect, init_db
from swaps.services import (cancel_allocation, claim_seat, open_due_releases,
                            release_now, set_release_mode)


def _sent(db):
    out = []
    open_due_releases(db, lambda f, e: out.append(sorted(e)))
    return out


def _setup(db, places=1):
    """Formal 1 with `places` cancelled places held (manual mode). Users 10/11
    hold places at formal 2 (general group); 20/21 have none (no-swaps group).
    All four subscribe to formal 1."""
    add_formal(db, 1, slots=places, dt="2035-01-01 19:30")
    add_formal(db, 2, slots=5, dt="2035-02-01 19:30")
    add_formal(db, 3, slots=5, dt="2035-03-01 19:30")
    for uid in (1, 2, 3, 10, 11, 20, 21):
        add_user(db, uid)
    set_release_mode(db, "manual")
    for uid in range(1, places + 1):
        claim_seat(db, uid, 1)
        cancel_allocation(db, uid, db.execute(
            "SELECT id FROM allocations WHERE user_id=? AND formal_id=1",
            (uid,)).fetchone()["id"])
    for uid in (10, 11):
        claim_seat(db, uid, 2)
    for uid in (10, 11, 20, 21):
        db.execute("INSERT INTO subscriptions(user_id, formal_id) VALUES (?,1)", (uid,))


NOSWAPS = ["u20@pem.cam.ac.uk", "u21@pem.cam.ac.uk"]
GENERAL = ["u10@pem.cam.ac.uk", "u11@pem.cam.ac.uk"]


def test_release_all_after_noswaps_skips_those_already_emailed(db):
    _setup(db)
    release_now(db, 1, everyone=False)
    assert _sent(db) == [NOSWAPS]
    # User 20 claims a place elsewhere in between: now counts as "general",
    # but was already told about this place -> not emailed again.
    claim_seat(db, 20, 3)
    release_now(db, 1, everyone=True)
    assert _sent(db) == [GENERAL]
    assert _sent(db) == []          # nothing left to send


def test_group_change_the_other_way_still_gets_emailed(db):
    _setup(db)
    release_now(db, 1, everyone=False)
    assert _sent(db) == [NOSWAPS]
    # User 10 gives up their only place: now "no-swaps", but was never told.
    cancel_allocation(db, 10, db.execute(
        "SELECT id FROM allocations WHERE user_id=10").fetchone()["id"])
    release_now(db, 1, everyone=True)
    assert _sent(db) == [GENERAL]


def test_release_all_straight_away_emails_everyone_once(db):
    _setup(db, places=3)
    release_now(db, 1, everyone=True)
    assert _sent(db) == [sorted(GENERAL + NOSWAPS)]   # one email each, not one per place


def test_backfill_for_places_half_released_before_tracking(tmp_path):
    path = str(tmp_path / "t.db")
    init_db(path)
    db = connect(path)
    _setup(db)
    release_now(db, 1, everyone=False)
    _sent(db)
    # Simulate a database from before release_emails existed.
    db.execute("DROP TABLE release_emails")
    db.close()
    init_db(path)
    db = connect(path)
    assert sorted(r[0] for r in db.execute("SELECT user_id FROM release_emails")) == [20, 21]
    release_now(db, 1, everyone=True)
    assert _sent(db) == [GENERAL]
