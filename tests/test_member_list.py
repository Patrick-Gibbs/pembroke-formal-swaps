import importlib
from datetime import datetime, timedelta

from conftest import add_formal, add_user
from swaps.db import connect, set_setting
from swaps.services import (blacklist_and_delete_user, email_key, is_blacklisted,
                            set_member_allowlist, users_not_on_allowlist)


def test_email_key():
    assert email_key("ABC12@cam.ac.uk") == email_key("abc12@pem.cam.ac.uk") == "crsid:abc12"
    assert email_key(" Friend@Gmail.com ") == "friend@gmail.com"


def test_allowlist_save_and_check(db):
    add_user(db, 1, "abc12@pem.cam.ac.uk")      # listed as @cam.ac.uk -> matches
    add_user(db, 2, "zzz99@joh.cam.ac.uk")      # not a member -> flagged
    add_user(db, 3, "boss@pem.cam.ac.uk")       # the swaps officer
    n, rejected = set_member_allowlist(db, "abc12@cam.ac.uk\n\nABC12@cam.ac.uk\nnot-an-email\n")
    assert n == 1 and rejected == ["not-an-email"]
    set_setting(db, "admin_email", "boss@pem.cam.ac.uk")
    flagged = {u["email"]: u for u in users_not_on_allowlist(db)}
    assert set(flagged) == {"zzz99@joh.cam.ac.uk", "boss@pem.cam.ac.uk"}
    assert flagged["boss@pem.cam.ac.uk"]["is_admin"]
    # saving replaces the list
    set_member_allowlist(db, "zzz99@cam.ac.uk")
    assert {u["email"] for u in users_not_on_allowlist(db)} == {
        "abc12@pem.cam.ac.uk", "boss@pem.cam.ac.uk"}


def test_blacklist_delete_releases_places_and_blocks(db):
    add_user(db, 1, "zzz99@joh.cam.ac.uk")
    db.execute("UPDATE users SET dietary_flags='Vegan' WHERE id=1")
    soon = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d %H:%M")
    add_formal(db, 1, slots=3, dt=soon)
    db.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (1,1,'active')")
    blacklist_and_delete_user(db, 1)
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    rel = db.execute("SELECT orig_name, orig_dietary FROM released_slots").fetchone()
    assert rel is not None                        # place went through a normal release
    assert rel["orig_name"] == "a member who cancelled" and rel["orig_dietary"] == "Vegan"
    assert is_blacklisted(db, "zzz99@cam.ac.uk")  # any cam.ac.uk form of the CRSid


def test_blacklist_blocks_registration_routes(db_path, monkeypatch):
    c = connect(db_path)
    c.execute("INSERT INTO email_blacklist(key, email) VALUES ('crsid:zzz99', 'zzz99@joh.cam.ac.uk')")
    c.execute("INSERT INTO email_blacklist(key, email) VALUES ('x@gmail.com', 'x@gmail.com')")
    c.commit(); c.close()
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    pub = app.test_client()
    with pub.session_transaction() as s:
        s["_csrf"] = "tok"
    pub.post("/register", data={"_csrf": "tok", "first_name": "Z", "last_name": "Z",
                                "email": "zzz99@cam.ac.uk", "pin": "4821"})
    adm = app.test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    adm.post("/admin/users/register", data={"_csrf": "tok", "first_name": "X",
                                            "last_name": "Y", "email": "x@gmail.com"})
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    c.close()
    r = adm.get("/admin/members?check=1")
    assert r.status_code == 200 and b"zzz99@joh.cam.ac.uk" in r.data
    adm.post("/admin/members/unblacklist", data={"_csrf": "tok", "key": "x@gmail.com"})
    adm.post("/admin/users/register", data={"_csrf": "tok", "first_name": "X",
                                            "last_name": "Y", "email": "x@gmail.com"})
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
