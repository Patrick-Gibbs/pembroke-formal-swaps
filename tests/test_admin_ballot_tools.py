import importlib

from conftest import add_formal, add_user
from swaps.db import connect, set_setting
from swaps.services import (ballot_groups_overview, simulate_ballot,
                            unranked_members)


def _rank(db, uid, fids, term="T1"):
    for i, f in enumerate(fids, 1):
        db.execute("INSERT INTO preferences(user_id, formal_id, rank, term) "
                   "VALUES (?,?,?,?)", (uid, f, i, term))


def _group(db, leader, members, term="T1", party=""):
    gid = db.execute("INSERT INTO ballot_groups(term, leader_user_id, party_name) "
                     "VALUES (?,?,?)", (term, leader, party)).lastrowid
    for m, st in [(leader, "accepted")] + members:
        db.execute("INSERT INTO ballot_group_members(group_id, user_id, status) "
                   "VALUES (?,?,?)", (gid, m, st))
    return gid


def test_unranked_members(db):
    for u in range(1, 7):
        add_user(db, u)
    add_formal(db, 1, slots=5)
    db.execute("UPDATE formals SET status='open'")
    _rank(db, 1, [1])                       # ranked -> excluded
    _group(db, 2, [(3, "accepted"), (4, "invited")])   # 2 leads, unranked
    set_setting(db, "admin_email", "u6@pem.cam.ac.uk") # officer -> excluded
    ids = {r["id"] for r in unranked_members(db, "T1")}
    # 2 = leader who hasn't ranked (needs reminding); 3 = covered by leader;
    # 4 = only invited, still solo -> reminded; 5 = never ranked.
    assert ids == {2, 4, 5}


def test_groups_overview(db):
    for u in (1, 2, 3):
        add_user(db, u)
    add_formal(db, 1, slots=5); add_formal(db, 2, slots=5)
    _rank(db, 1, [2, 1])
    _group(db, 1, [(2, "accepted"), (3, "invited")], party="Supper Club")
    g = ballot_groups_overview(db, "T1")[0]
    assert g["party_name"] == "Supper Club" and g["leader"] == "U1 Test"
    assert g["size"] == 2 and g["pending"] == 1
    assert g["ranking"] == ["College2", "College1"]
    assert g["members"][0]["is_leader"]


def test_simulate_ballot_aggregates(db):
    for u in range(1, 5):
        add_user(db, u)
    add_formal(db, 1, slots=1); add_formal(db, 2, slots=5)
    db.execute("UPDATE formals SET status='open'")
    for u in (1, 2, 3):
        _rank(db, u, [1, 2])                # 3 want formal 1 (1 seat), all fall back to 2
    _rank(db, 4, [2])
    sim = simulate_ballot(db, "T1", trials=50)
    assert sim["entrants"] == 4 and sim["pct_placed"] == 100
    f1 = next(f for f in sim["per_formal"] if f["college"] == "College1")
    assert f1["first_demand"] == 3 and f1["pressure"] == 3.0 and f1["pct_full"] == 100
    # each of users 1-3 gets formal 1 about a third of the time
    firsts = [u["pct_first"] for u in sim["per_unit"] if u["first_choice"] == "College1"]
    assert sum(firsts) == 100
    assert simulate_ballot(db, "NOPE") is None


def test_admin_pages_render(db_path, monkeypatch):
    conn = connect(db_path)
    for u in (1, 2):
        add_user(conn, u)
    add_formal(conn, 1, slots=3)
    conn.execute("UPDATE formals SET status='open'")
    _rank(conn, 1, [1])
    _group(conn, 1, [(2, "accepted")])
    set_setting(conn, "current_term", "T1")
    set_setting(conn, "term_ballot_close", "2099-01-01 18:00")
    conn.commit(); conn.close()
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["is_admin"] = True
        s["_csrf"] = "tok"
    for path, needle in [("/admin/groups", b"(leader)"),
                         ("/admin/simulate", b"By formal"),
                         ("/admin/remind-unranked", b"Remind members to rank")]:
        r = c.get(path)
        assert r.status_code == 200 and needle in r.data, path
