"""Background thread: opens due released slots and emails subscribers.
Runs inside the single waitress process (the systemd unit runs exactly one
process, so there is exactly one worker)."""
import logging
import threading
import time

from . import config, emailer
from .db import connect
from .ics import formal_ics
from .services import open_due_releases, send_scheduled_emails

log = logging.getLogger("swaps.release")

POLL_SECONDS = 30


def _fixed_meal(formal):
    """The one meal every open place at this formal is fixed to (host list
    already sent), or None if there's none or they differ."""
    if not formal["catering_sent"]:
        return None
    conn = connect()
    try:
        meals = {r["orig_dietary"] for r in conn.execute(
            "SELECT orig_dietary FROM released_slots WHERE formal_id=? "
            "AND claimed_by IS NULL AND opened=1", (formal["id"],))}
    finally:
        conn.close()
    return meals.pop() if len(meals) == 1 and None not in meals else None


def _notify(formal, emails):
    link = f"{config.SITE_URL}/formals/{formal['id']}/claim"
    meal = _fixed_meal(formal)
    for email in emails:
        emailer.slot_open_email(email, formal, link, meal)
    log.info("Released slot for formal %s (%s); notified %d subscriber(s)",
             formal["id"], formal["host_college"], len(emails))


def _send_reminders(formal, emails):
    ics = [(f"{formal['host_college']}-formal.ics",
            formal_ics(formal).encode())]
    for email in emails:
        emailer.reminder_email(email, formal, ics)
    log.info("Sent day-of reminder for formal %s to %d attendee(s)",
             formal["id"], len(emails))


def _send_reviews(formal, emails):
    link = f"{config.SITE_URL}/review/{formal['id']}"
    for email in emails:
        emailer.review_request_email(email, formal, link)
    log.info("Sent review request for formal %s to %d attendee(s)",
             formal["id"], len(emails))


def _send_catering(formal, to, cc, rows, admin_name):
    emailer.catering_email(to, cc, formal, rows, admin_name)
    log.info("Sent catering list for formal %s to %s (cc %s), %d attendee(s)",
             formal["id"], to, cc, len(rows))


def _loop():
    while True:
        try:
            conn = connect()
            try:
                open_due_releases(conn, _notify)
                send_scheduled_emails(conn, _send_reminders, _send_reviews,
                                      _send_catering)
            finally:
                conn.close()
        except Exception:
            log.exception("release worker iteration failed")
        time.sleep(POLL_SECONDS)


def start():
    t = threading.Thread(target=_loop, name="release-worker", daemon=True)
    t.start()
    return t
