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


def _notify(formal, emails):
    link = f"{config.SITE_URL}/formals/{formal['id']}/claim"
    for email in emails:
        emailer.slot_open_email(email, formal, link)
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


def _loop():
    while True:
        try:
            conn = connect()
            try:
                open_due_releases(conn, _notify)
                send_scheduled_emails(conn, _send_reminders, _send_reviews)
            finally:
                conn.close()
        except Exception:
            log.exception("release worker iteration failed")
        time.sleep(POLL_SECONDS)


def start():
    t = threading.Thread(target=_loop, name="release-worker", daemon=True)
    t.start()
    return t
