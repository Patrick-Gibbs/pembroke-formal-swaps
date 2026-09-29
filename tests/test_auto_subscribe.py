from conftest import add_formal, add_user
from swaps.services import auto_subscribe_unmet


def _subs(db):
    return {(r["user_id"], r["formal_id"]) for r in
            db.execute("SELECT user_id, formal_id FROM subscriptions")}


def _rank(db, uid, fids, term="T1"):
    for i, f in enumerate(fids, 1):
        db.execute("INSERT INTO preferences(user_id, formal_id, rank, term) "
                   "VALUES (?,?,?,?)", (uid, f, i, term))


def _hold(db, uid, fid):
    db.execute("INSERT INTO allocations(user_id, formal_id, status) "
               "VALUES (?,?,'active')", (uid, fid))


def test_subscribes_to_ranked_formals_not_won(db):
    for u in (1, 2):
        add_user(db, u)
    for f in (1, 2, 3):
        add_formal(db, f, slots=5)                  # 'allocated'
    _rank(db, 1, [1, 2, 3]); _hold(db, 1, 1); _hold(db, 1, 2)   # got 1st+2nd
    _rank(db, 2, [3]);                                          # got nothing
    assert auto_subscribe_unmet(db, "T1") == 2
    assert _subs(db) == {(1, 3), (2, 3)}


def test_respects_max_places_cap(db):
    add_user(db, 1)
    for f in (1, 2):
        add_formal(db, f, slots=5)
    _rank(db, 1, [1, 2]); _hold(db, 1, 1)
    db.execute("INSERT INTO ballot_caps(user_id, term, max_places) VALUES (1,'T1',1)")
    assert auto_subscribe_unmet(db, "T1") == 0     # wanted only 1 and has 1


def test_group_members_use_leader_ranking(db):
    for u in (1, 2):
        add_user(db, u)
    for f in (1, 2):
        add_formal(db, f, slots=5)
    _rank(db, 1, [1, 2])                            # leader's ranking
    _rank(db, 2, [2])                               # member's own: inert
    gid = db.execute("INSERT INTO ballot_groups(term, leader_user_id) "
                     "VALUES ('T1', 1)").lastrowid
    for u in (1, 2):
        db.execute("INSERT INTO ballot_group_members(group_id, user_id, status) "
                   "VALUES (?,?,'accepted')", (gid, u))
    _hold(db, 1, 1); _hold(db, 2, 1)                # group won formal 1
    auto_subscribe_unmet(db, "T1")
    assert _subs(db) == {(1, 2), (2, 2)}


def test_unsubscribe_not_undone_by_republish(db):
    add_user(db, 1)
    for f in (1, 2):
        add_formal(db, f, slots=5)
    _rank(db, 1, [1, 2]); _hold(db, 1, 1)
    assert auto_subscribe_unmet(db, "T1") == 1
    db.execute("DELETE FROM subscriptions WHERE user_id=1 AND formal_id=2")
    assert auto_subscribe_unmet(db, "T1") == 0     # member's choice respected
    assert _subs(db) == set()


def test_skips_formals_not_yet_allocated(db):
    add_user(db, 1)
    add_formal(db, 1, slots=5)
    db.execute("UPDATE formals SET status='open'")
    _rank(db, 1, [1])
    assert auto_subscribe_unmet(db, "T1") == 0
