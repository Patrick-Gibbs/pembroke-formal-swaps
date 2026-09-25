import pytest

from conftest import add_formal, add_user
from swaps.services import (SwapError, accept_swap, propose_swap, respond_swap)


def _held(db, uid, fid):
    return db.execute("SELECT 1 FROM allocations WHERE user_id=? AND formal_id=? "
                      "AND status='active'", (uid, fid)).fetchone() is not None


def _setup(db):
    for u in (1, 2):
        add_user(db, u)
    # far-future formals so the cutoff never blocks
    add_formal(db, 1, slots=5, dt="2035-01-01 19:30", term="T1")
    add_formal(db, 2, slots=5, dt="2035-02-01 19:30", term="T1")
    db.execute("UPDATE formals SET status='allocated'")
    db.execute("INSERT INTO allocations(user_id, formal_id, status, source) "
               "VALUES (1,1,'active','ballot')")  # user1 @ formal1
    db.execute("INSERT INTO allocations(user_id, formal_id, status, source) "
               "VALUES (2,2,'active','ballot')")  # user2 @ formal2


def test_full_swap_exchange(db):
    _setup(db)
    rid = propose_swap(db, 1, 1, 2, 2)   # user1 offers f1 for user2's f2
    accept_swap(db, rid, 2)
    assert _held(db, 1, 2) and not _held(db, 1, 1)  # user1 now at formal2
    assert _held(db, 2, 1) and not _held(db, 2, 2)  # user2 now at formal1
    assert db.execute("SELECT status FROM swap_requests WHERE id=?",
                      (rid,)).fetchone()["status"] == "accepted"


def test_cannot_swap_for_place_you_hold(db):
    _setup(db)
    db.execute("INSERT INTO allocations(user_id, formal_id, status, source) "
               "VALUES (1,2,'active','ballot')")  # user1 already at formal2
    with pytest.raises(SwapError):
        propose_swap(db, 1, 1, 2, 2)


def test_accept_fails_if_place_gone(db):
    _setup(db)
    rid = propose_swap(db, 1, 1, 2, 2)
    # user1 cancels their place before user2 accepts
    db.execute("UPDATE allocations SET status='cancelled' WHERE user_id=1 AND formal_id=1")
    with pytest.raises(SwapError):
        accept_swap(db, rid, 2)


def test_swap_allowed_close_to_formal_if_list_not_sent(db):
    # No fixed date cut-off any more: 2 days out is fine while the host list
    # hasn't gone out.
    for u in (1, 2):
        add_user(db, u)
    from datetime import datetime, timedelta
    soon = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    add_formal(db, 1, slots=5, dt=soon, term="T1")
    add_formal(db, 2, slots=5, dt="2035-02-01 19:30", term="T1")
    db.execute("UPDATE formals SET status='allocated'")
    db.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (1,1,'active')")
    db.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (2,2,'active')")
    assert propose_swap(db, 1, 1, 2, 2) > 0


def test_swap_blocked_once_either_dietary_list_sent(db):
    _setup(db)
    rid = propose_swap(db, 1, 1, 2, 2)          # proposed while both lists unsent
    db.execute("UPDATE formals SET catering_sent=1 WHERE id=2")
    with pytest.raises(SwapError):              # accept re-checks -> blocked
        accept_swap(db, rid, 2)
    with pytest.raises(SwapError):              # and new proposals too
        propose_swap(db, 1, 1, 2, 2)
    assert _held(db, 1, 1) and _held(db, 2, 2)  # nothing moved


def test_decline_and_cancel(db):
    _setup(db)
    rid = propose_swap(db, 1, 1, 2, 2)
    with pytest.raises(SwapError):
        respond_swap(db, rid, 1, "decline")   # proposer can't decline
    respond_swap(db, rid, 2, "decline")        # recipient can
    assert db.execute("SELECT status FROM swap_requests WHERE id=?",
                      (rid,)).fetchone()["status"] == "declined"
    rid2 = propose_swap(db, 1, 1, 2, 2)
    respond_swap(db, rid2, 1, "cancel")        # proposer cancels
    assert db.execute("SELECT status FROM swap_requests WHERE id=?",
                      (rid2,)).fetchone()["status"] == "cancelled"


def test_accept_cancels_conflicting_pending(db):
    _setup(db)
    add_user(db, 3)
    add_formal(db, 3, slots=5, dt="2035-03-01 19:30", term="T1")
    db.execute("UPDATE formals SET status='allocated'")
    db.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (3,3,'active')")
    r1 = propose_swap(db, 1, 1, 2, 2)   # user1's formal1 <-> user2 formal2
    r2 = propose_swap(db, 3, 3, 1, 1)   # user3 wants user1's formal1 too
    accept_swap(db, r1, 2)              # formal1 moves to user2
    # r2 (which relied on user1 holding formal1) is now stale -> cancelled
    assert db.execute("SELECT status FROM swap_requests WHERE id=?",
                      (r2,)).fetchone()["status"] == "cancelled"
