from conftest import add_formal, add_user


def test_expectation_stats_counts_only_completed_ballots(db):
    from swaps.views.main import _expectation_stats
    for u in (1, 2, 3):
        add_user(db, u)
    add_formal(db, 1, slots=5)                         # allocated
    add_formal(db, 2, slots=5)                         # allocated
    add_formal(db, 3, slots=5)
    db.execute("UPDATE formals SET host_college='Open College', status='open' WHERE id=3")
    for uid, fid, rk in [(1, 1, 1), (1, 2, 2), (2, 1, 1), (3, 2, 1), (3, 1, 2),
                         (1, 3, 3)]:                   # (1,3) = live ballot
        db.execute("INSERT INTO preferences(user_id, formal_id, rank, term) "
                   "VALUES (?,?,?,'T1')", (uid, fid, rk))
    db.execute("INSERT INTO reviews(user_id, formal_id, course_stars, vibe_stars) "
               "VALUES (1, 1, 3, 1)")
    s = {c["college"]: c for c in _expectation_stats(db)}
    assert "Open College" not in s                     # live demand never shown
    c1 = s["College1"]
    assert c1["firsts"] == 2 and c1["rankings"] == 3 and c1["avg_rank"] == 1.33
    assert c1["firsts_per_formal"] == 2 and c1["rating"] == 4 and c1["reviews"] == 1
    assert s["College2"]["rating"] is None and s["College2"]["firsts"] == 1
