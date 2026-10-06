import importlib

import pytest

from conftest import add_formal
from swaps.dates import from_uk, uk_date, uk_datetime
from swaps.db import connect, set_setting


def _app(db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    return app


def test_helpers():
    assert uk_datetime("2026-10-09 18:00") == "09/10/2026 18:00"
    assert uk_datetime("2026-10-09T18:00:59") == "09/10/2026 18:00"
    assert uk_datetime("2026-10-09") == "09/10/2026"
    assert uk_date("2026-10-09 18:00:00") == "09/10/2026"
    assert uk_datetime("") == "" and uk_datetime(None) is None
    assert uk_datetime("not a date") == "not a date"
    assert from_uk("27/11/2026 19:30") == "2026-11-27 19:30"
    assert from_uk("2026-11-27T19:30") == "2026-11-27 19:30"
    assert from_uk("") == ""
    with pytest.raises(ValueError):
        from_uk("11/27/2026 19:30")   # US order is rejected, not silently swapped


def test_pages_and_import_use_uk_dates(db_path, monkeypatch):
    c = connect(db_path)
    add_formal(c, 1, 5, dt="2035-05-10 19:30")
    set_setting(c, "current_term", "T1")
    c.commit(); c.close()
    app = _app(db_path, monkeypatch)
    adm = app.test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"

    page = adm.get("/admin/").data.decode()
    assert "10/05/2035 19:30" in page and "2035-05-10 19:30<" not in page
    assert 'lang="en-GB"' in page

    adm.post("/admin/formals/import", data={"_csrf": "tok", "csv":
             "host_college,dt,slots,ballot_close\nJesus,27/11/2035 19:30,4,20/11/2035 12:00\n"
             "Clare,31/02/2035 19:30,4,\n"})
    c = connect(db_path)
    rows = c.execute("SELECT host_college, dt, ballot_close FROM formals "
                     "WHERE host_college IN ('Jesus','Clare')").fetchall()
    c.close()
    assert [tuple(r) for r in rows] == [("Jesus", "2035-11-27 19:30", "2035-11-20 12:00")]
