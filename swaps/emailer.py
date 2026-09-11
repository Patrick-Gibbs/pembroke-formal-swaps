"""Single outbound channel: Resend. EMAIL_MODE=dev logs to stdout instead."""
import base64
import logging

import requests

from . import config

log = logging.getLogger("swaps.email")

RESEND_URL = "https://api.resend.com/emails"


def _record(to, subject, ok, error=""):
    """Append to the email_log table. Uses its own connection because sends
    often happen on background threads outside any request. Never raises."""
    try:
        from .db import connect
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO email_log(recipient, subject, ok, error) "
                "VALUES (?,?,?,?)", (to, subject[:300], int(ok), error[:500]))
        finally:
            conn.close()
    except Exception:
        log.exception("could not write email_log entry")


def send(to, subject, html, attachments=None):
    """Send one email. attachments: [(filename, bytes)]. Returns True on
    success. Never raises. Every attempt is recorded in email_log."""
    if config.EMAIL_MODE != "live":
        names = [a[0] for a in (attachments or [])]
        log.info("[dev email] to=%s subject=%r attachments=%s", to, subject, names)
        print(f"--- DEV EMAIL to={to} subject={subject!r} attachments={names} ---"
              f"\n{html}\n---", flush=True)
        _record(to, subject, True, "dev mode — not actually sent")
        return True
    payload = {"from": config.MAIL_FROM, "to": [to], "subject": subject, "html": html}
    if attachments:
        payload["attachments"] = [
            {"filename": name, "content": base64.b64encode(data).decode()}
            for name, data in attachments]
    try:
        r = requests.post(
            RESEND_URL, json=payload,
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            timeout=10,
        )
        if r.status_code // 100 != 2:
            log.error("Resend error %s for %s: %s", r.status_code, to, r.text[:500])
            _record(to, subject, False, f"HTTP {r.status_code}: {r.text[:300]}")
            return False
        _record(to, subject, True)
        return True
    except requests.RequestException as e:
        log.error("Resend request failed for %s: %s", to, e)
        _record(to, subject, False, str(e))
        return False


def _formal_block(f):
    parts = [f"<p><b>{f['host_college']}</b> — {f['dt']}"]
    if f["price"]:
        parts.append(f"<br>Price: {f['price']}")
    if f["location"]:
        parts.append(f"<br>Where: {f['location']}")
    if f["instructions"]:
        parts.append(f"<br>{f['instructions']}")
    parts.append("</p>")
    return "".join(parts)


def verification_email(to, link):
    return send(
        to, "Verify your email — Pembroke Formal Swaps",
        f"<p>Welcome to Pembroke Formal Swaps.</p>"
        f"<p><a href=\"{link}\">Click here to verify your email address</a> "
        f"(link valid for 24 hours).</p>"
        f"<p>If you didn't register, ignore this email.</p>")


def pin_reset_email(to, link):
    return send(
        to, "Reset your PIN — Pembroke Formal Swaps",
        f"<p>Someone (hopefully you) asked to reset the PIN for this account.</p>"
        f"<p><a href=\"{link}\">Choose a new PIN here</a> (link valid for 1 hour).</p>"
        f"<p>If this wasn't you, ignore this email — your PIN is unchanged.</p>")


def group_invite_email(to, leader_name, term, link):
    return send(
        to, f"{leader_name} invited you to a group ballot — Pembroke Formal Swaps",
        f"<p><b>{leader_name}</b> has invited you to ballot as a group for "
        f"formal swaps this term ({term}).</p>"
        f"<p>If you accept, {leader_name} sets the ranking for the whole group "
        f"and you'll all get the same formals together.</p>"
        f"<p><a href=\"{link}\">Accept or decline the invitation here</a>.</p>")


def slot_open_email(to, formal, link):
    return send(
        to, f"A place has opened: {formal['host_college']} formal on {formal['dt']}",
        f"<p>A place has just opened up for the <b>{formal['host_college']}</b> formal "
        f"on <b>{formal['dt']}</b>.</p>"
        f"<p><a href=\"{link}\">Claim it here</a> — first come, first served.</p>")


def allocation_result_email(to, formals, ics_files):
    """Ballot result for a winner. formals: rows; ics_files: [(name, bytes)]."""
    n = len(formals)
    blocks = "".join(_formal_block(f) for f in formals)
    return send(
        to,
        f"Your formal swap result{'s' if n > 1 else ''} — "
        + ", ".join(f["host_college"] for f in formals),
        f"<p>Good news — the ballot has run and you got "
        f"{'these formals' if n > 1 else 'a place at this formal'}:</p>"
        f"{blocks}"
        f"<p>Calendar invitations are attached — open one to add the formal to "
        f"your calendar.</p>"
        f"<p>Manage your places (or cancel, up until the cut-off) at "
        f"<a href=\"{config.SITE_URL}/me\">{config.SITE_URL}/me</a>.</p>",
        attachments=ics_files)


def no_place_email(to, term):
    return send(
        to, "Formal swap ballot result — Pembroke Formal Swaps",
        f"<p>The formal swap ballot for {term} has run, and unfortunately the "
        f"formals you ranked all filled up before your turn — you don't have a "
        f"place this time.</p>"
        f"<p>Places do open up when people cancel: use “Notify me” on any formal at "
        f"<a href=\"{config.SITE_URL}\">{config.SITE_URL}</a> and you'll get an "
        f"email the moment a place is released — first come, first served.</p>")


def reminder_email(to, formal, ics_files):
    review_link = f"{config.SITE_URL}/review/{formal['id']}"
    return send(
        to, f"Today: {formal['host_college']} formal at {formal['dt'][11:16]}",
        f"<p>A friendly reminder — you're going to a formal <b>today</b>:</p>"
        f"{_formal_block(formal)}"
        f"<p>Have a wonderful evening! And at the end of the formal, remember "
        f"to <a href=\"{review_link}\">leave a review</a> — it helps everyone "
        f"pick next term's swaps.</p>",
        attachments=ics_files)


def review_request_email(to, formal, link):
    return send(
        to, f"How was the {formal['host_college']} formal?",
        f"<p>Hope you enjoyed the <b>{formal['host_college']}</b> formal tonight!</p>"
        f"<p><a href=\"{link}\">Rate your experience out of 5 stars</a> — one star "
        f"per good course, two for the vibes — and leave a review or photo if you "
        f"like. It helps everyone pick next term's swaps.</p>")
