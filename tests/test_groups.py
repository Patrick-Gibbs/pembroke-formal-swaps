import random
from collections import Counter

from conftest import add_formal, add_user
from swaps.allocation import run_ballot
from swaps.services import run_allocation


def test_group_block_wins_or_loses_together():
    # A has 2 seats: either the pair takes both, or the solo takes one and the
    # pair (no longer fitting) falls through to B.
    formals = {1: 2, 2: 5}
    prefs = {"g:1": [1, 2], "u:9": [1, 2]}
    sizes = {"g:1": 2}
    for seed in range(20):
        a, _ = run_ballot(formals, prefs, seed, sizes)
        seats = Counter(f for _, f, _ in a)
        got = {u: f for u, f, r in a if r == 1}
        if got["g:1"] == 1:
            assert got["u:9"] == 2  # solo displaced to B
        else:
            assert got == {"u:9": 1, "g:1": 2}
        assert seats[1] <= 2 and seats[2] <= 5


def test_leftover_seats_go_to_smaller_unit_when_group_passed_over():
    # A has 3 seats; group of 3 vs two solos. If a solo is drawn first the
    # group can no longer fit, and the remaining seats go to solos.
    formals = {1: 3}
    prefs = {"g:1": [1], "u:8": [1], "u:9": [1]}
    sizes = {"g:1": 3}
    outcomes = set()
    for seed in range(30):
        a, _ = run_ballot(formals, prefs, seed, sizes)
        winners = frozenset(u for u, f, _ in a if f == 1)
        used = sum(3 if u == "g:1" else 1 for u in winners)
        assert used <= 3
        outcomes.add(winners)
    assert frozenset({"g:1"}) in outcomes          # group sometimes wins whole
    assert frozenset({"u:8", "u:9"}) in outcomes   # sometimes solos split it


def test_group_too_big_for_every_formal_gets_nothing():
    formals = {1: 2, 2: 2}
    prefs = {"g:1": [1, 2], "u:9": [1]}
    sizes = {"g:1": 4}
    a, _ = run_ballot(formals, prefs, "s", sizes)
    assert all(u != "g:1" for u, _, _ in a)
    assert ("u:9", 1, 1) in a


def test_group_invariants_random_instances():
    rng = random.Random(7)
    for case in range(20):
        formals = {f: rng.randint(1, 6) for f in range(1, rng.randint(2, 5))}
        prefs, sizes = {}, {}
        for u in range(1, rng.randint(3, 15)):
            uid = f"u:{u}" if rng.random() < 0.7 else f"g:{u}"
            sizes[uid] = 1 if uid.startswith("u") else rng.randint(2, 4)
            prefs[uid] = rng.sample(list(formals), rng.randint(1, len(formals)))
        a, _ = run_ballot(formals, prefs, f"case{case}", sizes)
        again, _ = run_ballot(formals, prefs, f"case{case}", sizes)
        assert a == again
        used = Counter()
        for u, f, _ in a:
            used[f] += sizes[u]
        for f, n in used.items():
            assert n <= formals[f], "formal seated more people than slots"
        # empty-handed units must have no still-fitting ranked formal
        got = {u for u, _, _ in a}
        for u, plist in prefs.items():
            if u not in got:
                assert all(formals[f] - used[f] < sizes[u] for f in plist)


def test_db_run_allocation_group_shares_outcome(db):
    for uid in (1, 2, 3, 4):
        add_user(db, uid)
    add_formal(db, 1, slots=3, term="T1")
    add_formal(db, 2, slots=1, term="T1")
    db.execute("UPDATE formals SET status='open' WHERE id IN (1,2)")
    # group: leader 1 + members 2,3 (accepted); user 4 solo
    db.execute("INSERT INTO ballot_groups(id, term, leader_user_id) VALUES (1,'T1',1)")
    for uid in (1, 2, 3):
        db.execute("INSERT INTO ballot_group_members(group_id, user_id, status) "
                   "VALUES (1, ?, 'accepted')", (uid,))
    # leader ranks [1, 2]; member 2's own ranking must be ignored; solo ranks [1, 2]
    for rank, fid in enumerate([1, 2], 1):
        db.execute("INSERT INTO preferences(user_id, formal_id, rank, term) "
                   "VALUES (1, ?, ?, 'T1')", (fid, rank))
        db.execute("INSERT INTO preferences(user_id, formal_id, rank, term) "
                   "VALUES (4, ?, ?, 'T1')", (fid, rank))
    db.execute("INSERT INTO preferences(user_id, formal_id, rank, term) "
               "VALUES (2, 2, 1, 'T1')")

    _run, seed, placed, _log, new_allocs = run_allocation(db, "T1", seed="grouptest")
    assert sorted(new_allocs) == [(1, 1), (2, 1), (3, 1), (4, 2)]
    rows = db.execute("SELECT user_id, formal_id FROM allocations "
                      "WHERE status='active' ORDER BY user_id").fetchall()
    by_user = {r["user_id"]: r["formal_id"] for r in rows}
    # group of 3 only fits formal 1; whole group must sit together there
    assert by_user[1] == by_user[2] == by_user[3] == 1
    # solo user 4: formal 1 is full, cascades to formal 2
    assert by_user[4] == 2
    assert placed == 4


def test_caps_limit_extra_places():
    # Plenty of capacity: without caps both users would get 3 formals each.
    formals = {1: 2, 2: 2, 3: 2}
    prefs = {"u:1": [1, 2, 3], "u:2": [1, 2, 3]}
    from collections import Counter
    a, _ = run_ballot(formals, prefs, "cap", caps={"u:1": 1, "u:2": 3})
    got = Counter(u for u, _, _ in a)
    assert got["u:1"] == 1 and got["u:2"] == 3


def test_group_cap_is_min_of_members(db):
    for uid in (1, 2, 3):
        add_user(db, uid)
    add_formal(db, 1, slots=4, term="T1")
    add_formal(db, 2, slots=4, term="T1")
    db.execute("UPDATE formals SET status='open'")
    db.execute("INSERT INTO ballot_groups(id, term, leader_user_id) VALUES (1,'T1',1)")
    for uid in (1, 2):
        db.execute("INSERT INTO ballot_group_members(group_id, user_id, status) "
                   "VALUES (1, ?, 'accepted')", (uid,))
    # member 2 is only happy with 1 swap -> group capped at 1
    db.execute("INSERT INTO ballot_caps(user_id, term, max_places) VALUES (2,'T1',1)")
    for rank, fid in enumerate([1, 2], 1):
        db.execute("INSERT INTO preferences(user_id, formal_id, rank, term) "
                   "VALUES (1, ?, ?, 'T1')", (fid, rank))
    _r, _s, placed, _l, new_allocs = run_allocation(db, "T1", seed="capgrp")
    assert placed == 2  # the pair got exactly ONE formal (2 seats), not two
    assert {f for _, f in new_allocs} == {1}
