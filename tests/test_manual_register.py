import importlib

from swaps.db import connect


def _app(db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    return app


def _user(db_path, email):
    c = connect(db_path)
    u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    c.close()
    return u


def test_manual_registration_flow(db_path, monkeypatch):
    app = _app(db_path, monkeypatch)
    admin = app.test_client()
    with admin.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    email = "Friend@Gmail.com"
    admin.post("/admin/users/register", data={
        "_csrf": "tok", "first_name": "Jon", "last_name": "Smiht", "email": email})
    u = _user(db_path, "friend@gmail.com")
    assert u and u["email_verified"] == 1 and u["manual_registered"] == 1
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM email_log WHERE recipient='friend@gmail.com' "
                     "AND subject LIKE 'You''re registered%'").fetchone()[0] == 1
    c.close()
    # duplicate refused
    admin.post("/admin/users/register", data={
        "_csrf": "tok", "first_name": "X", "last_name": "Y", "email": email})
    assert _user(db_path, "friend@gmail.com")["first_name"] == "Jon"

    # member logs in with the default PIN
    m = app.test_client()
    with m.session_transaction() as s:
        s["_csrf"] = "tok"
    r = m.post("/login", data={"_csrf": "tok", "email": email, "pin": "1234"})
    assert r.status_code == 302
    with m.session_transaction() as s:
        s["_csrf"] = "tok"

    # one-time name fix
    m.post("/account/name", data={"_csrf": "tok", "first_name": "Jon", "last_name": "Smith"})
    m.post("/account/name", data={"_csrf": "tok", "first_name": "Nope", "last_name": "Nope"})
    u = _user(db_path, "friend@gmail.com")
    assert (u["first_name"], u["last_name"], u["name_changed"]) == ("Jon", "Smith", 1)

    # dietary edit
    m.post("/account/dietary", data={"_csrf": "tok", "diet_Vegan": "on",
                                     "dietary_other": "no nuts"})
    u = _user(db_path, "friend@gmail.com")
    assert u["dietary_flags"] == "Vegan" and u["dietary_other"] == "no nuts"

    # PIN change: wrong current / default PIN refused, then a real change
    from swaps.security import verify_secret
    m.post("/account/pin", data={"_csrf": "tok", "current_pin": "9999",
                                 "new_pin": "4821", "confirm_pin": "4821"})
    m.post("/account/pin", data={"_csrf": "tok", "current_pin": "1234",
                                 "new_pin": "1234", "confirm_pin": "1234"})
    assert verify_secret(_user(db_path, "friend@gmail.com")["pin_hash"], "1234")
    m.post("/account/pin", data={"_csrf": "tok", "current_pin": "1234",
                                 "new_pin": "4821", "confirm_pin": "4821"})
    assert verify_secret(_user(db_path, "friend@gmail.com")["pin_hash"], "4821")

    # deleting the account frees the email; the admin can register them again
    m.post("/account/delete", data={"_csrf": "tok", "pin": "4821"})
    assert _user(db_path, "friend@gmail.com") is None
    admin.post("/admin/users/register", data={
        "_csrf": "tok", "first_name": "Jon", "last_name": "Smith", "email": email})
    assert _user(db_path, "friend@gmail.com")["name_changed"] == 0


def test_self_registered_members_cannot_use_name_fix(db_path, monkeypatch):
    from conftest import add_user
    c = connect(db_path); add_user(c, 1); c.commit(); c.close()
    app = _app(db_path, monkeypatch)
    m = app.test_client()
    with m.session_transaction() as s:
        s["uid"] = 1; s["_csrf"] = "tok"
    m.post("/account/name", data={"_csrf": "tok", "first_name": "A", "last_name": "B"})
    c = connect(db_path)
    assert c.execute("SELECT first_name FROM users WHERE id=1").fetchone()[0] == "U1"
