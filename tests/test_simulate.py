from conftest import add_formal, add_user
from swaps.services import simulate_user


def _prefs(db, user_id, term, fids):
    for rank, fid in enumerate(fids, 1):
        db.execute("INSERT INTO preferences(user_id, formal_id, rank, term) "
                   "VALUES (?,?,?,?)", (user_id, fid, rank, term))


def test_uncontested_first_pref_is_certain(db):
    add_user(db, 1)
    add_formal(db, 1, slots=5, term="T1")
    db.execute("UPDATE formals SET status='open'")
    _prefs(db, 1, "T1", [1])
    r = simulate_user(db, 1, "T1", trials=50)
    assert r["first_pref_pct"] == 100
    assert r["expected_total"] == 1.0
    assert r["in_group"] is False
    assert r["n_entrants"] == 1


def test_slot_distributions(db):
    add_user(db, 1)
    for fid in (1, 2, 3):
        add_formal(db, fid, slots=5, term="T1")
    db.execute("UPDATE formals SET status='open'")
    _prefs(db, 1, "T1", [1, 2, 3])
    # cap defaults to 3, everything uncontested -> 3 guaranteed swaps in order
    r = simulate_user(db, 1, "T1", trials=25)
    assert len(r["slots"]) == 3
    assert r["slots"][0]["none_pct"] == 0 and r["slots"][0]["options"][0]["pct"] == 100
    assert r["slots"][2]["none_pct"] == 0  # third swap certain here
    assert r["expected_total"] == 3.0


def test_cap_limits_shown_slots(db):
    add_user(db, 1)
    for fid in (1, 2, 3):
        add_formal(db, fid, slots=5, term="T1")
    db.execute("UPDATE formals SET status='open'")
    _prefs(db, 1, "T1", [1, 2, 3])
    db.execute("INSERT INTO ballot_caps(user_id, term, max_places) VALUES (1,'T1',1)")
    r = simulate_user(db, 1, "T1", trials=10)
    assert len(r["slots"]) == 1  # capped at one swap -> only the first slot shown
    assert r["expected_total"] == 1.0


def test_oversubscribed_first_pref_is_split(db):
    for u in (1, 2):
        add_user(db, u)
    add_formal(db, 1, slots=1, term="T1")  # one seat, both want it
    db.execute("UPDATE formals SET status='open'")
    _prefs(db, 1, "T1", [1])
    _prefs(db, 2, "T1", [1])
    r1 = simulate_user(db, 1, "T1", trials=100)
    r2 = simulate_user(db, 2, "T1", trials=100)
    # exactly one of them wins the single seat each trial
    assert 0 < r1["first_pref_pct"] < 100
    assert r1["first_pref_pct"] + r2["first_pref_pct"] == 100


def test_non_entrant_returns_none(db):
    add_user(db, 1)
    add_formal(db, 1, slots=5, term="T1")
    db.execute("UPDATE formals SET status='open'")
    assert simulate_user(db, 1, "T1", trials=10) is None  # no preferences saved


def test_group_member_sees_group_outcome(db):
    for u in (1, 2):
        add_user(db, u)
    add_formal(db, 1, slots=5, term="T1")
    db.execute("UPDATE formals SET status='open'")
    db.execute("INSERT INTO ballot_groups(id, term, leader_user_id) VALUES (1,'T1',1)")
    for u in (1, 2):
        db.execute("INSERT INTO ballot_group_members(group_id, user_id, status) "
                   "VALUES (1, ?, 'accepted')", (u,))
    _prefs(db, 1, "T1", [1])  # leader's ranking governs the group
    r = simulate_user(db, 2, "T1", trials=20)  # the non-leader member
    assert r["in_group"] is True
    assert r["first_pref_pct"] == 100
