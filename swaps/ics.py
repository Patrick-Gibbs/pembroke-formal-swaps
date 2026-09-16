"""Minimal iCalendar (.ics) generation for formal invitations. Attached to
emails so one tap adds the event in Google/Apple/Outlook calendars."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

LONDON = ZoneInfo(config.TIMEZONE)
EVENT_HOURS = 3  # assumed length of a formal


def _esc(s):
    return (s.replace("\\", "\\\\").replace(";", "\\;")
             .replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n"))


def _utc(dt_local):
    return dt_local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _vevent(formal):
    """VEVENT lines for one formal (dict/Row with id, host_college, dt, price,
    location, instructions)."""
    start = datetime.fromisoformat(formal["dt"].replace("T", " ")).replace(tzinfo=LONDON)
    end = start + timedelta(hours=EVENT_HOURS)
    location = formal["location"] or f"{formal['host_college']} College, Cambridge"
    desc_parts = []
    if formal["price"]:
        desc_parts.append(f"Price: {formal['price']}")
    if formal["instructions"]:
        desc_parts.append(formal["instructions"])
    desc_parts.append(f"Organised via {config.SITE_URL}")
    return [
        "BEGIN:VEVENT",
        f"UID:formal-{formal['id']}@pembrokeformalswaps.com",
        f"DTSTAMP:{_utc(datetime.now(LONDON))}",
        f"DTSTART:{_utc(start)}",
        f"DTEND:{_utc(end)}",
        f"SUMMARY:{_esc('Formal at ' + formal['host_college'])}",
        f"LOCATION:{_esc(location)}",
        f"DESCRIPTION:{_esc(chr(10).join(desc_parts))}",
        "BEGIN:VALARM",
        "TRIGGER:-PT2H",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_esc('Formal at ' + formal['host_college'] + ' soon')}",
        "END:VALARM",
        "END:VEVENT",
    ]


def _wrap(events, method="PUBLISH", name=None):
    head = ["BEGIN:VCALENDAR", "VERSION:2.0",
            "PRODID:-//Pembroke Formal Swaps//EN", f"METHOD:{method}"]
    if name:
        head += [f"X-WR-CALNAME:{_esc(name)}"]
    return "\r\n".join(head + events + ["END:VCALENDAR"]) + "\r\n"


def formal_ics(formal):
    """Single-event .ics for email attachments."""
    return _wrap(_vevent(formal))


def feed_ics(formals, name="My formal swaps"):
    """Multi-event calendar feed a user can subscribe to (auto-updating)."""
    events = []
    for f in formals:
        events += _vevent(f)
    return _wrap(events, name=name)
