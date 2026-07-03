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


def test_cancelled_seat_held_until_release_then_claimable(db):
    add_user(db, 1); add_user(db, 2)
    add_formal(db, 1, slots=1)
    claim_seat(db, 1, 1)
    rng = random.Random(7)
    cancel_allocation(db, 1, db.execute(
        "SELECT id FROM allocations WHERE user_id=1").fetchone()["id"], rng=rng)
    row = db.execute("SELECT release_at FROM released_slots").fetchone()
    delay = (datetime.fromisoformat(row["release_at"]).replace(tzinfo=timezone.utc)
             - datetime.now(timezone.utc)).total_seconds()
    assert -5 <= delay <= 3600, "release must be within 0-60 minutes"

    if delay > 1:  # seat still held: nobody can grab it yet
        assert free_seats(db, 1) == 0
        with pytest.raises(ClaimError):
            claim_seat(db, 2, 1)

    # Time passes: force the hold to expire.
    db.execute("UPDATE released_slots SET release_at=?", (utc(-60),))
    assert free_seats(db, 1) == 1
    notified = []
    db.execute("INSERT INTO subscriptions(user_id, formal_id) VALUES (2, 1)")
    opened = open_due_releases(db, lambda f, emails: notified.append(emails))
    assert opened == 1
    assert notified == [["u2@pem.cam.ac.uk"]]
    assert open_due_releases(db, lambda f, e: notified.append(e)) == 0  # once only
    claim_seat(db, 2, 1)
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
