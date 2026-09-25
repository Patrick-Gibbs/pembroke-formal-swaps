import random
import threading
from datetime import datetime, timedelta, timezone

import pytest

from conftest import add_formal, add_user
from swaps.db import connect
from swaps.services import (CancelError, ClaimError, cancel_allocation,
                            claim_seat, free_seats, open_due_releases)


def utc(delta_s=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)
            ).strftime("%Y-%m-%d %H:%M:%S")


def test_claim_takes_last_seat_once(db):
    add_user(db, 1); add_user(db, 2)
    add_formal(db, 1, slots=1)
    claim_seat(db, 1, 1)
    with pytest.raises(ClaimError):
        claim_seat(db, 2, 1)
    assert free_seats(db, 1) == 0


def test_claim_rejects_duplicate(db):
    add_user(db, 1)
    add_formal(db, 1, slots=5)
    claim_seat(db, 1, 1)
    with pytest.raises(ClaimError):
        claim_seat(db, 1, 1)


def test_concurrent_claims_one_winner(db_path):
    setup = connect(db_path)
    add_user(setup, 1); add_user(setup, 2)
    add_formal(setup, 1, slots=1)
    setup.close()

    results = {}
    barrier = threading.Barrier(2)

    def worker(uid):
        conn = connect(db_path)
        try:
            barrier.wait()
            claim_seat(conn, uid, 1)
            results[uid] = "won"
        except ClaimError:
            results[uid] = "lost"
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(u,)) for u in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == ["lost", "won"], results
    check = connect(db_path)
    n = check.execute("SELECT COUNT(*) n FROM allocations WHERE status='active'"
                      ).fetchone()["n"]
    check.close()
    assert n == 1


def test_cutoffs_within_24h(db):
    add_user(db, 1)
    soon = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")
    far = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d %H:%M")
    add_formal(db, 1, slots=2, dt=soon)
    add_formal(db, 2, slots=2, dt=far)
    with pytest.raises(ClaimError):
        claim_seat(db, 1, 1)
    claim_seat(db, 1, 2)
    alloc = db.execute("SELECT id FROM allocations WHERE user_id=1 AND formal_id=2"
                       ).fetchone()
    db.execute("UPDATE formals SET dt=? WHERE id=2", (soon,))
    with pytest.raises(CancelError):
        cancel_allocation(db, 1, alloc["id"])
    db.execute("UPDATE formals SET dt=? WHERE id=2", (far,))
    cancel_allocation(db, 1, alloc["id"])  # fine again >24h out


def test_priority_window_two_phase(db):
    add_user(db, 1); add_user(db, 2); add_user(db, 3)
    add_formal(db, 1, slots=1)
    add_formal(db, 2, slots=1)
    claim_seat(db, 1, 1)            # user1 holds the only seat at formal 1
    claim_seat(db, 3, 2)            # user3 already has a place (formal 2) -> general
    cancel_allocation(db, 1, db.execute(
        "SELECT id FROM allocations WHERE user_id=1 AND formal_id=1").fetchone()["id"])
    # Force the priority window open now, general phase still in the future.
    db.execute("UPDATE released_slots SET release_at=?, general_at=?",
               (utc(-60), utc(3600)))
    # user2 missed everything -> priority audience sees it free and may claim;
    # the general audience (user3) does not yet.
    assert free_seats(db, 1, priority=True) == 1
    assert free_seats(db, 1) == 0
    with pytest.raises(ClaimError):
        claim_seat(db, 3, 1)       # general user blocked during the head start

    # Notifications: priority phase emails only the missed-all subscribers.
    db.execute("INSERT INTO subscriptions(user_id, formal_id) VALUES (2, 1)")
    db.execute("INSERT INTO subscriptions(user_id, formal_id) VALUES (3, 1)")
    notified = []
    assert open_due_releases(db, lambda f, e: notified.append(e)) == 1
    assert notified == [["u2@pem.cam.ac.uk"]]  # user3 (has a place) waits

    claim_seat(db, 2, 1)           # priority user claims within the window
    assert free_seats(db, 1) == 0


def test_general_phase_after_head_start(db):
    add_user(db, 1); add_user(db, 3)
    add_formal(db, 1, slots=1); add_formal(db, 2, slots=1)
    claim_seat(db, 1, 1)
    claim_seat(db, 3, 2)           # user3 holds a place -> general audience
    cancel_allocation(db, 1, db.execute(
        "SELECT id FROM allocations WHERE user_id=1 AND formal_id=1").fetchone()["id"])
    # Both phases now in the past.
    db.execute("UPDATE released_slots SET release_at=?, general_at=?",
               (utc(-7200), utc(-60)))
    assert free_seats(db, 1) == 1  # general audience can now take it
    db.execute("INSERT INTO subscriptions(user_id, formal_id) VALUES (3, 1)")
    notified = []
    open_due_releases(db, lambda f, e: notified.extend(e))
    assert "u3@pem.cam.ac.uk" in notified
    claim_seat(db, 3, 1)           # general user claims after the head start
    assert free_seats(db, 1) == 0


def test_subscriber_already_attending_not_notified(db):
    add_user(db, 1); add_user(db, 2)
    add_formal(db, 1, slots=2)
    claim_seat(db, 1, 1)
    claim_seat(db, 2, 1)
    db.execute("INSERT INTO subscriptions(user_id, formal_id) VALUES (2, 1)")
    cancel_allocation(db, 1, db.execute(
        "SELECT id FROM allocations WHERE user_id=1").fetchone()["id"])
    db.execute("UPDATE released_slots SET release_at=?", (utc(-60),))
    notified = []
    open_due_releases(db, lambda f, emails: notified.extend(emails))
    assert notified == []  # user 2 already has an active place


def test_release_timing_head_start_invariant(db):
    import random as _random
    from datetime import datetime as _dt
    add_user(db, 1)
    add_formal(db, 1, slots=1)
    claim_seat(db, 1, 1)
    aid = db.execute("SELECT id FROM allocations WHERE user_id=1").fetchone()["id"]
    # Many seeds: general open must always be >= 2h after the priority open.
    for seed in range(30):
        db.execute("DELETE FROM released_slots")
        db.execute("UPDATE allocations SET status='active' WHERE id=?", (aid,))
        cancel_allocation(db, 1, aid, rng=_random.Random(seed))
        r = db.execute("SELECT release_at, general_at FROM released_slots").fetchone()
        rel = _dt.fromisoformat(r["release_at"])
        gen = _dt.fromisoformat(r["general_at"])
        assert (gen - rel).total_seconds() >= 2 * 3600, seed


def test_claim_inherits_dietary_after_list_sent(db):
    add_user(db, 1); add_user(db, 2)
    add_formal(db, 1, slots=1)
    # user1 has dietary; they hold the seat
    db.execute("UPDATE users SET dietary_flags='Vegan', dietary_other='no nuts' WHERE id=1")
    claim_seat(db, 1, 1)
    # host list already sent for this formal
    db.execute("UPDATE formals SET catering_sent=1")
    aid = db.execute("SELECT id FROM allocations WHERE user_id=1").fetchone()["id"]
    cancel_allocation(db, 1, aid)
    db.execute("UPDATE released_slots SET release_at=?, general_at=?", (utc(-60), utc(-60)))
    # user2 (different dietary) claims -> inherits user1's frozen meal
    db.execute("UPDATE users SET dietary_flags='Halal' WHERE id=2")
    info = claim_seat(db, 2, 1)
    assert info is not None
    assert info["replaced"] == "U1 Test"
    assert "Vegan" in info["dietary"] and "no nuts" in info["dietary"]
    # roster / catering shows the frozen dietary, not user2's own
    from swaps.services import catering_list
    cl = catering_list(db, 1)
    assert cl[0]["dietary"] == info["dietary"]
    a = db.execute("SELECT inherited_dietary, inherited_from FROM allocations "
                   "WHERE user_id=2 AND formal_id=1").fetchone()
    assert a["inherited_from"] == "U1 Test"


def test_no_inheritance_before_list_sent(db):
    add_user(db, 1); add_user(db, 2)
    add_formal(db, 1, slots=1)
    db.execute("UPDATE users SET dietary_flags='Vegan' WHERE id=1")
    claim_seat(db, 1, 1)
    aid = db.execute("SELECT id FROM allocations WHERE user_id=1").fetchone()["id"]
    cancel_allocation(db, 1, aid)   # catering_sent still 0
    db.execute("UPDATE released_slots SET release_at=?, general_at=?", (utc(-60), utc(-60)))
    db.execute("UPDATE users SET dietary_flags='Halal' WHERE id=2")
    info = claim_seat(db, 2, 1)
    assert info is None             # list not sent -> claimer keeps own dietary
    from swaps.services import catering_list
    assert catering_list(db, 1)[0]["dietary"] == "Halal"
