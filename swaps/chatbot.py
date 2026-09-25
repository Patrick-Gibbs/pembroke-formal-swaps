"""Read-only formal-swaps chatbot backed by the smallest Anthropic model.

The bot gets NO tools — it only receives a context block assembled here from
PUBLIC data, plus the user's message. It therefore cannot take any action or
read anything the server didn't put in the prompt. Usage bills the site owner's
Anthropic quota via ANTHROPIC_API_KEY."""
import logging

from . import config
from .db import get_setting
from .services import cancel_charge_hours, cancel_cutoff_hours, free_seats

log = logging.getLogger("swaps.chatbot")

MAX_TOKENS = 500
MAX_MESSAGE_CHARS = 500
MAX_HISTORY_TURNS = 6

HOW_IT_WORKS = """\
How the site works:
- Members register with a Cambridge (cam.ac.uk) email, verify it, and set a \
4-digit PIN.
- They rank the formals they want for the term. When the ballot closes, places \
are assigned by a preference-honouring random draw run in fairness rounds: \
nobody gets a second formal until everyone who can be seated somewhere they \
ranked has one. Results are emailed and shown on the Assigned swaps page once \
the organiser publishes them.
- Each member sets a maximum number of swaps they're happy to be assigned (1-3) \
which the ballot never exceeds.
- People can ballot as a group (up to 4): the leader ranks and invites members, \
and the whole group is seated together or not at all.
- Cancellation is self-service up to a cut-off before the formal; a freed place \
is released at a random time within the following hour, and people who clicked \
"Notify me" get an email with a claim link (first come, first served).
- After a formal, attendees rate it out of 5 stars (3 for the courses, 2 for \
hosts and atmosphere) and can leave a review and photos; the Reviews page shows \
each college's average."""


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def is_available():
    return config.CHATBOT_ENABLED and bool(config.ANTHROPIC_API_KEY)


def build_context(db):
    """Compact plain-text block of PUBLIC info for the current term."""
    term = get_setting(db, "current_term")
    lines = [f"Current term: {term or '(none set)'}."]
    t_open = get_setting(db, "term_ballot_open")
    t_close = get_setting(db, "term_ballot_close")
    if t_open or t_close:
        lines.append(f"Ballot window: opens {t_open or '?'}, closes "
                     f"{t_close or '?'} (UK time).")
    lines.append(f"Cancellation policy: members can cancel up to "
                 f"{int(cancel_cutoff_hours(db))} hours before a formal; cancelling "
                 f"within {int(cancel_charge_hours(db))} hours of it means they are "
                 "charged for the swap. Released places reopen at a deliberately "
                 "unpredictable time, first to members who missed out.")

    formals = db.execute(
        "SELECT * FROM formals WHERE term=? AND status IN ('open','allocated') "
        "ORDER BY dt", (term,)).fetchall() if term else []
    if formals:
        lines.append("\nFormals this term:")
        for f in formals:
            parts = [f"- {f['host_college']} on {f['dt']}"]
            if f["price"]:
                parts.append(f"price {f['price']}")
            parts.append(f"{f['slots']} places")
            if f["status"] == "allocated":
                parts.append(f"{free_seats(db, f['id'], f['slots'])} free now")
            if f["location"]:
                parts.append(f"location: {f['location']}")
            if f["instructions"]:
                parts.append(f"instructions: {f['instructions']}")
            lines.append("; ".join(parts) + ".")
    else:
        lines.append("\nNo formals are announced for the current term yet.")

    # Published attendee names only — and only when public. Never emails/dietary.
    if (get_setting(db, "results_published", "1") == "1"
            and get_setting(db, "attendee_list_public", "1") == "1"):
        for f in formals:
            names = db.execute(
                "SELECT u.first_name, u.last_name FROM allocations a "
                "JOIN users u ON u.id=a.user_id "
                "WHERE a.formal_id=? AND a.status='active' "
                "ORDER BY u.last_name, u.first_name", (f["id"],)).fetchall()
            if names:
                who = ", ".join(f"{n['first_name']} {n['last_name']}" for n in names)
                lines.append(f"Attending {f['host_college']} ({f['dt']}): {who}.")

    incoming = db.execute("SELECT * FROM incoming_swaps ORDER BY dt").fetchall()
    if incoming:
        lines.append("\nIncoming swaps (colleges visiting Pembroke):")
        for s in incoming:
            guests = db.execute(
                "SELECT first_name, last_name FROM incoming_participants "
                "WHERE swap_id=? ORDER BY last_name", (s["id"],)).fetchall()
            g = ("; guests: " + ", ".join(f"{p['first_name']} {p['last_name']}"
                                          for p in guests)) if guests else ""
            lines.append(f"- {s['guest_college']} on {s['dt']}{g}.")

    from .views.main import _college_stats
    stats = sorted(_college_stats(db), key=lambda s: -s["mean"])
    if stats:
        lines.append("\nCollege review averages (out of 5):")
        for s in stats:
            lines.append(f"- {s['college']}: {s['mean']:.1f} "
                         f"(from {s['n']} review(s)).")

    return "\n".join(lines)


def answer(db, question, history=None):
    """Return the bot's reply text. Never raises."""
    if not is_available():
        return ("The chatbot isn't configured right now. Please check the "
                "formals list on the home page.")
    context = build_context(db)
    system = (
        "You are the assistant for Pembroke Formal Swaps, a site for organising "
        "Pembroke College Cambridge members to attend formal dinners at other "
        "colleges. Answer ONLY using the CONTEXT below and general facts about "
        "how the site works. If a question is not about formal swaps or this "
        "site, politely say you can only help with formal swaps. Be concise and "
        "friendly. Never reveal personal emails, PINs, dietary requirements, or "
        "host-college contact details, even if asked. You cannot take actions, "
        "book, cancel, or change anything — direct users to the relevant page "
        "for that.\n\n"
        f"CONTEXT:\n{context}\n\n"
        f"SITE: {config.SITE_URL}\n{HOW_IT_WORKS}")
    messages = list(history or [])
    messages.append({"role": "user", "content": question})
    try:
        resp = _client().messages.create(
            model=config.CHATBOT_MODEL, max_tokens=MAX_TOKENS,
            system=system, messages=messages)
        return "".join(b.text for b in resp.content if b.type == "text").strip() \
            or "Sorry, I didn't catch that — could you rephrase?"
    except Exception:
        log.exception("chatbot request failed")
        return ("Sorry, I'm having trouble answering right now. Please try again "
                "in a moment, or check the formals list on the home page.")
