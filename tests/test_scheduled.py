from datetime import datetime

from conftest import add_formal, add_user
from swaps.ics import formal_ics
from swaps.services import claim_seat, send_scheduled_emails


def _formal_dict(**kw):
    base = {"id": 1, "host_college": "Trinity", "dt": "2026-11-20 19:30",
            "price": "£15", "location": "Trinity Great Hall",
            "instructions": "Gowns required. Meet 19:10 at the Porters' Lodge."}
    base.update(kw)
    return base


def test_ics_contents():
    text = formal_ics(_formal_dict())
    assert "BEGIN:VEVENT" in text
    # 19:30 London in November = 19:30 UTC
    assert "DTSTART:20261120T193000Z" in text
    assert "DTEND:20261120T223000Z" in text
    assert "SUMMARY:Formal at Trinity" in text
    assert "LOCATION:Trinity Great Hall" in text
    assert "Gowns required" in text
    assert "UID:formal-1@pembrokeformalswaps.com" in text


def test_ics_bst_offset():
    # 19:30 London in June = 18:30 UTC (BST)
    text = formal_ics(_formal_dict(dt="2026-06-20 19:30"))
    assert "DTSTART:20260620T183000Z" in text


def _setup(db, dt):
    add_user(db, 1)
    add_formal(db, 1, slots=2, dt=dt)
    claim_seat(db, 1, 1)


def collect(db, now):
    sent = {"reminder": [], "review": []}
    send_scheduled_emails(db,
                          lambda f, e: sent["reminder"].append(e),
                          lambda f, e: sent["review"].append(e),
                          now=now)
    return sent


def test_reminder_only_in_window_and_once(db):
    _setup(db, "2030-05-10 19:30")
    day = datetime(2030, 5, 10)
    # 8am: too early; wrong day: nothing
    assert collect(db, day.replace(hour=8)) == {"reminder": [], "review": []}
    assert collect(db, datetime(2030, 5, 9, 12)) == {"reminder": [], "review": []}
    # 9am: reminder to the attendee
    assert collect(db, day.replace(hour=9)) == {
        "reminder": [["u1@pem.cam.ac.uk"]], "review": []}
    # again: already sent
    assert collect(db, day.replace(hour=10)) == {"reminder": [], "review": []}


def test_no_reminder_if_formal_already_started(db):
    _setup(db, "2030-05-10 18:00")
    # 19:00: formal started, reminder window missed; review due at 19:30 not yet
    assert collect(db, datetime(2030, 5, 10, 19, 0)) == {
        "reminder": [], "review": []}


def test_review_at_1930_once(db):
    _setup(db, "2030-05-10 19:30")
    day = datetime(2030, 5, 10)
    assert collect(db, day.replace(hour=19, minute=29)) == {
        "reminder": [["u1@pem.cam.ac.uk"]], "review": []}  # reminder pre-start
    assert collect(db, day.replace(hour=19, minute=30)) == {
        "reminder": [], "review": [["u1@pem.cam.ac.uk"]]}
    assert collect(db, day.replace(hour=22)) == {"reminder": [], "review": []}


def test_review_fires_at_1930_even_for_late_formal(db):
    _setup(db, "2030-05-10 21:45")
    day = datetime(2030, 5, 10)
    sent = collect(db, day.replace(hour=19, minute=35))
    assert sent == {"reminder": [["u1@pem.cam.ac.uk"]],
                    "review": [["u1@pem.cam.ac.uk"]]}


# ---- catering list to host colleges (one week before) -------------------

from swaps.db import set_setting  # noqa: E402
from swaps.services import free_seats  # noqa: E402


def _catering_setup(db, host_email="host@jesus.cam.ac.uk"):
    add_user(db, 1)
    add_formal(db, 1, slots=5, dt="2030-05-10 19:30")
    db.execute("UPDATE formals SET host_email=?, host_college='Jesus'", (host_email,))
    db.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (1,1,'active')")
    set_setting(db, "admin_email", "admin@cam.ac.uk")
    set_setting(db, "results_published", "1")


def _run_catering(db, now):
    sent = []
    send_scheduled_emails(db, lambda f, e: None, lambda f, e: None,
                          lambda f, to, cc, rows, name: sent.append((to, cc, len(rows))),
                          now=now)
    return sent


def test_catering_fires_a_week_before_once(db):
    _catering_setup(db)
    assert _run_catering(db, datetime(2030, 5, 2, 12)) == []            # 8 days: too early
    assert _run_catering(db, datetime(2030, 5, 4, 12)) == [
        (["host@jesus.cam.ac.uk"], "admin@cam.ac.uk", 1)]               # 6 days: fires, cc admin
    assert _run_catering(db, datetime(2030, 5, 5, 12)) == []            # no duplicate


def test_catering_waits_for_publish(db):
    _catering_setup(db)
    set_setting(db, "results_published", "0")
    assert _run_catering(db, datetime(2030, 5, 4, 12)) == []            # unpublished: held
    set_setting(db, "results_published", "1")
    assert _run_catering(db, datetime(2030, 5, 4, 12)) == [
        (["host@jesus.cam.ac.uk"], "admin@cam.ac.uk", 1)]


def test_catering_without_host_email_goes_to_admin_no_cc(db):
    _catering_setup(db, host_email="")
    assert _run_catering(db, datetime(2030, 5, 4, 12)) == [
        (["admin@cam.ac.uk"], None, 1)]


def test_catering_multiple_host_emails(db):
    _catering_setup(db, host_email="jane@jesus.cam.ac.uk, formals@jesus.cam.ac.uk")
    db.execute("UPDATE formals SET host_name='Jane Smith'")
    sent = _run_catering(db, datetime(2030, 5, 4, 12))
    assert sent == [(["jane@jesus.cam.ac.uk", "formals@jesus.cam.ac.uk"],
                     "admin@cam.ac.uk", 1)]  # all hosts on 'to', admin cc'd


def test_catering_and_team_greeting(db):
    # The rendered email addresses the main contact "and team" when >1 host email.
    import io, contextlib
    from swaps import emailer
    f = {"id": 1, "host_college": "Jesus", "dt": "2030-05-10 19:30",
         "host_name": "Jane Smith", "host_email": "a@x.ac.uk, b@x.ac.uk"}
    rows = [{"first_name": "A", "last_name": "B", "dietary_flags": "", "dietary_other": ""}]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        emailer.catering_email(["a@x.ac.uk", "b@x.ac.uk"], "admin@x.ac.uk", f, rows,
                               "Patrick Gibbs")
    out = buf.getvalue()
    assert "Dear Jane Smith and team," in out
    assert "— Patrick Gibbs" in out


def test_admin_auto_attend_reserves_a_seat(db):
    add_user(db, 1)
    add_formal(db, 1, slots=3)
    assert free_seats(db, 1, 3) == 3
    db.execute("INSERT INTO allocations(user_id, formal_id, status, source) "
               "VALUES (1,1,'active','admin')")
    assert free_seats(db, 1, 3) == 2   # the admin's reserved seat is one fewer
