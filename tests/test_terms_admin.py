import importlib

from conftest import add_formal, add_user
from swaps.db import connect, get_setting, set_setting
from swaps.terms import all_terms, is_published, make_current, term_setting


def _admin(db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    adm = app.test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    return adm


def _setup(db_path):
    c = connect(db_path)
    set_setting(c, "current_term", "Mich")
    set_setting(c, "results_published", "1")
    set_setting(c, "term_ballot_close", "2035-01-01 12:00")
    add_formal(c, 1, slots=2, dt="2035-01-10 19:30", term="Mich")
    add_user(c, 1)
    c.close()


def test_new_term_dropdown_and_per_term_actions(db_path, monkeypatch):
    _setup(db_path)
    adm = _admin(db_path, monkeypatch)
    adm.post("/admin/term/new", data={"_csrf": "tok", "name": "Lent"})
    c = connect(db_path)
    assert all_terms(c)[:2] == ["Lent", "Mich"]
    assert get_setting(c, "current_term") == "Mich"          # members unaffected
    page = adm.get("/admin/").data.decode()
    assert '<option value="Lent" selected>' in page and "Not the current term" in page
    assert "No formals in Lent yet" in page and "College1" not in page
    assert "Lent results are in <b>DRAFT</b>" in page
    # Formals created now default to Lent; its window is its own.
    assert 'value="Lent"' in adm.get("/admin/formals/new").data.decode()
    adm.post("/admin/term/settings", data={"_csrf": "tok", "term_ballot_open": "2035-03-01T09:00",
                                           "term_ballot_close": "2035-03-05T12:00"})
    assert term_setting(c, "term_ballot_close", "Lent") == "2035-03-05 12:00"
    assert get_setting(c, "term_ballot_close") == "2035-01-01 12:00"   # current untouched
    # Ballot + publish act on Lent only.
    add_formal(c, 2, slots=2, dt="2035-03-10 19:30", term="Lent")
    c.execute("INSERT INTO preferences(user_id, formal_id, rank, term) VALUES (1, 2, 1, 'Lent')")
    adm.post("/admin/allocate", data={"_csrf": "tok", "seed": "1"})
    assert c.execute("SELECT COUNT(*) FROM allocations WHERE formal_id=2").fetchone()[0] == 1
    assert not is_published(c, "Lent") and is_published(c, "Mich")
    adm.post("/admin/publish", data={"_csrf": "tok"})
    assert is_published(c, "Lent")
    # Switch back to Mich in the dropdown.
    adm.post("/admin/term/select", data={"_csrf": "tok", "term": "Mich"})
    page = adm.get("/admin/").data.decode()
    assert '<option value="Mich" selected>' in page and "College1" in page
    assert "<b>Current term</b>" in page


def test_make_current_swaps_what_members_see(db_path, monkeypatch):
    _setup(db_path)
    adm = _admin(db_path, monkeypatch)
    adm.post("/admin/term/new", data={"_csrf": "tok", "name": "Lent"})
    adm.post("/admin/term/settings", data={"_csrf": "tok", "term_ballot_open": "",
                                           "term_ballot_close": "2035-03-05T12:00"})
    adm.post("/admin/term/make-current", data={"_csrf": "tok", "term": "Lent"})
    c = connect(db_path)
    assert get_setting(c, "current_term") == "Lent"
    assert get_setting(c, "results_published") == "0"
    assert get_setting(c, "term_ballot_close") == "2035-03-05 12:00"
    # Michaelmas kept its own values and can be made current again.
    assert term_setting(c, "term_ballot_close", "Mich") == "2035-01-01 12:00"
    make_current(c, "Mich")
    assert get_setting(c, "results_published") == "1"
    assert get_setting(c, "term_ballot_close") == "2035-01-01 12:00"


def test_create_term_rejects_duplicates_and_blank(db_path, monkeypatch):
    _setup(db_path)
    adm = _admin(db_path, monkeypatch)
    r = adm.post("/admin/term/new", data={"_csrf": "tok", "name": "Mich"}, follow_redirects=True)
    assert "already exists" in r.data.decode()
    r = adm.post("/admin/term/new", data={"_csrf": "tok", "name": "  "}, follow_redirects=True)
    assert "Give the term a name" in r.data.decode()


def test_catering_follows_formals_own_term(db):
    from swaps.terms import set_term_setting
    set_setting(db, "current_term", "Mich")
    set_setting(db, "results_published", "1")
    set_term_setting(db, "results_published", "Lent", "0")
    assert is_published(db, "Mich") and not is_published(db, "Lent")
    assert is_published(db, "Old")          # legacy terms count as published
