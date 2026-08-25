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
    _setup(db, "2030-05-10 19:30")
    assert collect(db, datetime(2030, 5, 10, 20, 0)) == {
        "reminder": [], "review": []}  # 8pm: reminder window missed, review not due


def test_review_at_9pm_once(db):
    _setup(db, "2030-05-10 19:30")
    day = datetime(2030, 5, 10)
    assert collect(db, day.replace(hour=20, minute=59)) == {"reminder": [], "review": []}
    assert collect(db, day.replace(hour=21)) == {
        "reminder": [], "review": [["u1@pem.cam.ac.uk"]]}
    assert collect(db, day.replace(hour=22)) == {"reminder": [], "review": []}


def test_late_formal_review_waits_for_start(db):
    _setup(db, "2030-05-10 21:45")
    day = datetime(2030, 5, 10)
    # 21:00, formal at 21:45: reminder still goes (formal not started); the
    # review request must NOT fire before the formal has begun.
    assert collect(db, day.replace(hour=21)) == {
        "reminder": [["u1@pem.cam.ac.uk"]], "review": []}
    sent = collect(db, day.replace(hour=21, minute=50))
    assert sent == {"reminder": [], "review": [["u1@pem.cam.ac.uk"]]}
