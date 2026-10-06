import importlib

from conftest import add_formal, add_user
from swaps.db import connect, set_setting


def test_user_detail_pages(db_path, monkeypatch):
    c = connect(db_path)
    for u in (1, 2, 3):
        add_user(c, u)
    add_formal(c, 1, slots=5); add_formal(c, 2, slots=5)
    for uid, fid, rk in [(1, 2, 1), (1, 1, 2), (2, 1, 1)]:
        c.execute("INSERT INTO preferences(user_id, formal_id, rank, term) VALUES (?,?,?,'T1')",
                  (uid, fid, rk))
    gid = c.execute("INSERT INTO ballot_groups(term, leader_user_id, party_name) "
                    "VALUES ('T1', 1, 'Supper Club')").lastrowid
    for m in (1, 2):
        c.execute("INSERT INTO ballot_group_members(group_id, user_id, status) "
                  "VALUES (?,?,'accepted')", (gid, m))
    set_setting(c, "current_term", "T1")
    c.commit(); c.close()
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    a = app.test_client()
    with a.session_transaction() as s:
        s["is_admin"] = True
    users = a.get("/admin/users").data
    assert b'href="/admin/users/1"' in users
    lead = a.get("/admin/users/1").data.decode()          # leader: own ranking
    assert "Leads the group" in lead and lead.index("College2") < lead.index("College1")
    member = a.get("/admin/users/2").data.decode()        # member: leader's ranking
    assert "leader's</b> ranking" in member and "ignored while in the group" in member
    assert "No formals ranked" in a.get("/admin/users/3").data.decode()
    assert a.get("/admin/users/999").status_code == 302
