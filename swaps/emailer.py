"""Single outbound channel: Resend. EMAIL_MODE=dev logs to stdout instead."""
import logging

import requests

from . import config

log = logging.getLogger("swaps.email")

RESEND_URL = "https://api.resend.com/emails"


def send(to, subject, html):
    """Send one email. Returns True on success. Never raises."""
    if config.EMAIL_MODE != "live":
        log.info("[dev email] to=%s subject=%r\n%s", to, subject, html)
        print(f"--- DEV EMAIL to={to} subject={subject!r} ---\n{html}\n---", flush=True)
        return True
    try:
        r = requests.post(
            RESEND_URL,
            json={"from": config.MAIL_FROM, "to": [to], "subject": subject, "html": html},
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            timeout=10,
        )
        if r.status_code // 100 != 2:
            log.error("Resend error %s for %s: %s", r.status_code, to, r.text[:500])
            return False
        return True
    except requests.RequestException as e:
        log.error("Resend request failed for %s: %s", to, e)
        return False


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
