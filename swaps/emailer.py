"""Single outbound channel: Resend. EMAIL_MODE=dev logs to stdout instead."""
import base64
import logging
import re
import time

import requests

from . import config

log = logging.getLogger("swaps.email")

RESEND_URL = "https://api.resend.com/emails"


def split_emails(s):
    """Parse a free-text field of one or more emails (comma / semicolon /
    whitespace separated) into a de-duplicated list, order preserved."""
    out, seen = [], set()
    for part in re.split(r"[,;\s]+", (s or "").strip()):
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return out


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


def send(to, subject, html, attachments=None, cc=None, reply_to=None):
    """Send one email. `to`/`cc` may be a str or a list of addresses.
    reply_to: address replies should go to (instead of the no-reply sender).
    attachments: [(filename, bytes)]. Returns True on success. Never raises.
    Every attempt is recorded in email_log."""
    to_list = [to] if isinstance(to, str) else list(to)
    cc_list = [cc] if isinstance(cc, str) and cc else (cc or [])
    rec = ", ".join(to_list)
    if config.EMAIL_MODE != "live":
        names = [a[0] for a in (attachments or [])]
        log.info("[dev email] to=%s cc=%s reply_to=%s subject=%r attachments=%s",
                 rec, cc_list, reply_to, subject, names)
        print(f"--- DEV EMAIL to={rec} cc={cc_list} reply_to={reply_to} "
              f"subject={subject!r} "
              f"attachments={names} ---\n{html}\n---", flush=True)
        _record(rec, subject, True, "dev mode — not actually sent")
        return True
    payload = {"from": config.MAIL_FROM, "to": to_list, "subject": subject,
               "html": html}
    if cc_list:
        payload["cc"] = cc_list
    if reply_to:
        payload["reply_to"] = reply_to
    if attachments:
        payload["attachments"] = [
            {"filename": name, "content": base64.b64encode(data).decode()}
            for name, data in attachments]
    try:
        for attempt in range(4):
            r = requests.post(
                RESEND_URL, json=payload,
                headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
                timeout=10,
            )
            if r.status_code != 429 or attempt == 3:
                break
            time.sleep(1 + attempt)   # rate-limited: back off and retry
        if r.status_code // 100 != 2:
            log.error("Resend error %s for %s: %s", r.status_code, rec, r.text[:500])
            _record(rec, subject, False, f"HTTP {r.status_code}: {r.text[:300]}")
            return False
        _record(rec, subject, True)
        return True
    except requests.RequestException as e:
        log.error("Resend request failed for %s: %s", rec, e)
        _record(rec, subject, False, str(e))
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


# --- Admin-editable templates -------------------------------------------
# Each has a default subject/body (Python str.format placeholders) that the
# admin can override on the Email templates page. Security-sensitive emails
# (verification, PIN reset) are deliberately NOT editable.
TEMPLATES = {
    "slot_open": {
        "label": "Slot opened (to subscribers)",
        "placeholders": ["host_college", "dt", "link"],
        "subject": "A place has opened: {host_college} formal on {dt}",
        "body": "<p>A place has just opened up for the <b>{host_college}</b> "
                "formal on <b>{dt}</b>.</p>"
                "<p><a href=\"{link}\">Claim it here</a> — first come, first served.</p>",
    },
    "reminder": {
        "label": "Day-of reminder (to attendees, 9am)",
        "placeholders": ["host_college", "dt", "time", "formal_block", "review_link"],
        "subject": "Today: {host_college} formal at {time}",
        "body": "<p>A friendly reminder — you're going to a formal <b>today</b>:</p>"
                "{formal_block}"
                "<p>Have a wonderful evening! And at the end of the formal, remember "
                "to <a href=\"{review_link}\">leave a review</a>.</p>",
    },
    "review_request": {
        "label": "Review request (to attendees, 7:30pm)",
        "placeholders": ["host_college", "link"],
        "subject": "How was the {host_college} formal?",
        "body": "<p>Hope you enjoyed the <b>{host_college}</b> formal tonight!</p>"
                "<p><a href=\"{link}\">Rate your experience out of 5 stars</a> — one "
                "star per good course, two for the vibes — and leave a review or "
                "photo if you like.</p>",
    },
    "catering": {
        "label": "Attendance list to host college (1 week before)",
        "placeholders": ["greeting", "host_college", "dt", "count", "table",
                         "summary", "signoff"],
        "subject": "Pembroke attendees — {host_college} formal, {dt}",
        "body": "<p>{greeting}</p>"
                "<p>Please find below the <b>{count}</b> Pembroke College member(s) "
                "attending your formal on <b>{dt}</b>, with dietary requirements "
                "for catering.</p>{table}"
                "<p><b>Dietary summary:</b> {summary}.</p>"
                "<p>Please let us know if you need anything further. Thank you!</p>"
                "<p>{signoff}</p>",
    },
    "no_place": {
        "label": "No place this time (ballot miss)",
        "placeholders": ["term", "site"],
        "subject": "Formal swap ballot result — Pembroke Formal Swaps",
        "body": "<p>The formal swap ballot for {term} has run, and unfortunately the "
                "formals you ranked all filled up before your turn — you don't have a "
                "place this time.</p>"
                "<p>Places do open up when people cancel: use “Notify me” on any formal "
                "at <a href=\"{site}\">{site}</a> and you'll get an email the moment a "
                "place is released — first come, first served.</p>",
    },
    "rank_reminder": {
        "label": "Reminder to rank before the ballot closes (admin-sent)",
        "placeholders": ["first_name", "term", "deadline", "link"],
        "subject": "Reminder: rank your formals before {deadline}",
        "body": "<p>Hi {first_name},</p>"
                "<p>The formal swap ballot for {term} closes on <b>{deadline}</b>, and "
                "you haven't ranked any formals yet.</p>"
                "<p><a href=\"{link}\">Rank your formals now</a> — it only takes a "
                "minute. Rank only the ones you'd like to go to, most wanted first.</p>"
                "<p>Going with friends? Set up a ballot group so you win or lose "
                "together.</p>",
    },
}


def _render_template(key, ctx):
    """(subject, html) for a template, using the admin override if present and
    valid, else the code default. Bad overrides fall back to the default so a
    typo can never break sending."""
    d = TEMPLATES[key]
    subject_t, body_t = d["subject"], d["body"]
    try:
        from .db import connect
        conn = connect()
        try:
            row = conn.execute("SELECT subject, body FROM email_templates WHERE key=?",
                               (key,)).fetchone()
        finally:
            conn.close()
        if row and (row["subject"].strip() or row["body"].strip()):
            subject_t = row["subject"] or subject_t
            body_t = row["body"] or body_t
    except Exception:
        log.exception("could not load email template %s", key)
    try:
        return subject_t.format(**ctx), body_t.format(**ctx)
    except Exception:
        log.exception("bad override for template %s — using default", key)
        return d["subject"].format(**ctx), d["body"].format(**ctx)


def verification_email(to, link):
    return send(
        to, "Verify your email — Pembroke Formal Swaps",
        f"<p>Welcome to Pembroke Formal Swaps.</p>"
        f"<p><a href=\"{link}\">Click here to verify your email address</a> "
        f"(link valid for 24 hours).</p>"
        f"<p>If you didn't register, ignore this email.</p>")


def manual_registration_email(to, first_name, pin):
    """Sent when the swaps officer registers someone by hand. Not an editable
    template: it carries login credentials, like the verification/reset emails."""
    from html import escape
    site = config.SITE_URL
    return send(
        to, "You're registered — Pembroke Formal Swaps",
        f"<p>Hi {escape(first_name)},</p>"
        f"<p>The Pembroke swaps officer has registered you for "
        f"<b>Pembroke Formal Swaps</b>, where you can enter the ballot for formal "
        f"swaps at other colleges.</p>"
        f"<p>Your login details:<br>"
        f"Username (your email): <b>{escape(to)}</b><br>"
        f"PIN: <b>{escape(pin)}</b></p>"
        f"<p><a href=\"{site}/login\">Log in here</a>, then:</p>"
        f"<ul>"
        f"<li><b>Set your preferences</b> — rank the formals you'd like to go to on "
        f"<a href=\"{site}/rank\">My ranking</a>, most wanted first.</li>"
        f"<li><b>Check your dietary requirements</b> — add or edit them under "
        f"<a href=\"{site}/me\">My formals</a> → Your details.</li>"
        f"<li><b>Change your PIN</b> — please choose your own 4-digit PIN in the same "
        f"place, so nobody else can log in as you.</li>"
        f"</ul>")


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
    subject, body = _render_template("slot_open", {
        "host_college": formal["host_college"], "dt": formal["dt"], "link": link})
    return send(to, subject, body)


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


def rank_reminder_email(to, first_name, term, deadline):
    subject, body = _render_template("rank_reminder", {
        "first_name": first_name, "term": term, "deadline": deadline,
        "link": f"{config.SITE_URL}/rank"})
    return send(to, subject, body)


def no_place_email(to, term):
    subject, body = _render_template("no_place", {"term": term, "site": config.SITE_URL})
    return send(to, subject, body)


def reminder_email(to, formal, ics_files):
    subject, body = _render_template("reminder", {
        "host_college": formal["host_college"], "dt": formal["dt"],
        "time": formal["dt"][11:16], "formal_block": _formal_block(formal),
        "review_link": f"{config.SITE_URL}/review/{formal['id']}"})
    return send(to, subject, body, attachments=ics_files)


def swap_proposed_email(to, proposer_name, offer_formal, want_formal, link):
    return send(
        to, f"{proposer_name} wants to swap formals with you",
        f"<p><b>{proposer_name}</b> would like to swap places with you:</p>"
        f"<p>They give you their place at <b>{offer_formal['host_college']}</b> "
        f"({offer_formal['dt']}) in exchange for your place at "
        f"<b>{want_formal['host_college']}</b> ({want_formal['dt']}).</p>"
        f"<p><a href=\"{link}\">Review the request</a> to accept or decline.</p>")


def swap_accepted_email(to, other_name, now_attending, gave_up):
    return send(
        to, f"Swap confirmed — you're now at {now_attending['host_college']}",
        f"<p>Your swap with <b>{other_name}</b> is done.</p>"
        f"<p>You now have a place at <b>{now_attending['host_college']}</b> "
        f"({now_attending['dt']}), and gave up your place at "
        f"<b>{gave_up['host_college']}</b> ({gave_up['dt']}).</p>"
        f"<p>See your places at <a href=\"{config.SITE_URL}/me\">{config.SITE_URL}/me</a>.</p>")


def catering_email(to, cc, formal, rows, admin_name=""):
    """Attendance list + dietary requirements to a host college, one week before.
    rows: sequence with first_name, last_name, dietary_flags, dietary_other.
    Addressed to the host contact by name if known; signed by admin_name.
    Replies go to the cc'd admin, not the no-reply sender."""
    try:
        host_name = (formal["host_name"] or "").strip()
    except (KeyError, IndexError):
        host_name = ""
    try:
        multi = len(split_emails(formal["host_email"])) > 1
    except (KeyError, IndexError):
        multi = False
    if host_name:
        greeting = f"Dear {host_name} and team," if multi else f"Dear {host_name},"
    else:
        greeting = f"Dear {formal['host_college']} formals team,"
    signoff = f"— {admin_name}" if admin_name else "— Pembroke Formal Swaps"
    td = "<td style='border:1px solid #ccc;padding:4px 8px'>"
    th = "<th style='border:1px solid #ccc;padding:4px 8px;text-align:left'>"
    wine = any(r.get("wine") is not None for r in rows)
    body = "".join(
        f"<tr>{td}{r['first_name']} {r['last_name']}</td>{td}{r['dietary']}</td>"
        + (f"{td}{r['wine']}</td>" if wine else "") + "</tr>"
        for r in rows)
    tally = {}
    for r in rows:
        if r["dietary"] and r["dietary"] != "—":
            tally[r["dietary"]] = tally.get(r["dietary"], 0) + 1
    summary = ", ".join(f"{d} ×{n}" for d, n in sorted(tally.items())) or "none noted"
    if wine:
        yes = sum(1 for r in rows if r.get("wine") == "Yes")
        summary += f". Wine: {yes} yes, {len(rows) - yes} no"
    table = ("<table style='border-collapse:collapse'>"
             f"<tr>{th}Name</th>{th}Dietary requirements</th>"
             + (f"{th}Wine</th>" if wine else "") + "</tr>" + body + "</table>")
    subject, html = _render_template("catering", {
        "greeting": greeting, "host_college": formal["host_college"],
        "dt": formal["dt"], "count": len(rows), "table": table,
        "summary": summary, "signoff": signoff})
    return send(to, subject, html, cc=cc, reply_to=cc)


def claim_inherited_email(to, formal, replaced, dietary):
    return send(
        to, f"You're in — {formal['host_college']} formal on {formal['dt']}",
        f"<p>You've claimed a place at the <b>{formal['host_college']}</b> formal "
        f"on <b>{formal['dt']}</b>, in place of <b>{replaced}</b>.</p>"
        f"<p>The host college has already been given the final catering list, so "
        f"your meal is fixed to the one ordered for this place:</p>"
        f"<p><b>Dietary / meal: {dietary}</b></p>"
        f"<p>If that won't work for you, please contact the swaps officer as soon "
        f"as possible. Manage your places at "
        f"<a href=\"{config.SITE_URL}/me\">{config.SITE_URL}/me</a>.</p>")


def review_request_email(to, formal, link):
    return send(
        to, f"How was the {formal['host_college']} formal?",
        f"<p>Hope you enjoyed the <b>{formal['host_college']}</b> formal tonight!</p>"
        f"<p><a href=\"{link}\">Rate your experience out of 5 stars</a> — one star "
        f"per good course, two for the vibes — and leave a review or photo if you "
        f"like.</p>")
