from conftest import add_formal, add_user
from swaps.db import set_setting
from swaps.services import (admin_reserved_counts, free_seats, run_allocation,
                            seat_admin, unseat_admin)


def _enable(db, admin_uid=1):
    # user `admin_uid` is registered with the admin email
    db.execute("UPDATE users SET email='admin@cam.ac.uk' WHERE id=?", (admin_uid,))
    set_setting(db, "admin_email", "admin@cam.ac.uk")
    set_setting(db, "admin_auto_attend", "1")


def test_seat_admin_reserves_one_everywhere(db):
    add_user(db, 1)  # will be the admin member
    add_formal(db, 1, slots=10, term="T1")
    add_formal(db, 2, slots=6, term="T1")
    db.execute("UPDATE formals SET status='open'")
    _enable(db)
    assert seat_admin(db, "T1") == 2          # seated in both
    assert seat_admin(db, "T1") == 0          # idempotent
    assert free_seats(db, 1, 10) == 9         # N−1 available to members
    assert free_seats(db, 2, 6) == 5
    assert admin_reserved_counts(db, "T1") == {1: 1, 2: 1}


def test_seat_admin_needs_a_registered_member(db):
    add_formal(db, 1, slots=5, term="T1")
    db.execute("UPDATE formals SET status='open'")
    set_setting(db, "admin_email", "nobody@cam.ac.uk")
    set_setting(db, "admin_auto_attend", "1")
    assert seat_admin(db, "T1") == -1         # no verified member matches


def test_unseat_admin_restores_capacity(db):
    add_user(db, 1)
    add_formal(db, 1, slots=8, term="T1")
    db.execute("UPDATE formals SET status='open'")
    _enable(db)
    seat_admin(db, "T1")
    assert free_seats(db, 1, 8) == 7
    set_setting(db, "admin_auto_attend", "0")
    assert unseat_admin(db, "T1") == 1
    assert free_seats(db, 1, 8) == 8


def test_admin_in_every_formal_after_ballot(db):
    add_user(db, 1); add_user(db, 2)   # 1 = admin member, 2 = a ranker
    add_formal(db, 1, slots=2, term="T1")
    db.execute("UPDATE formals SET status='open'")
    _enable(db)
    seat_admin(db, "T1")
    db.execute("INSERT INTO preferences(user_id, formal_id, rank, term) VALUES (2,1,1,'T1')")
    run_allocation(db, "T1", seed="x")
    # admin holds the formal, and the ballot filled the remaining 1 seat (user 2)
    holders = {r["user_id"] for r in db.execute(
        "SELECT user_id FROM allocations WHERE formal_id=1 AND status='active'")}
    assert holders == {1, 2}
    assert free_seats(db, 1, 2) == 0
