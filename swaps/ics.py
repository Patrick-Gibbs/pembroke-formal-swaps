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


def formal_ics(formal):
    """Return .ics file text for one formal (dict/Row with host_college, dt,
    price, location, instructions)."""
    start = datetime.fromisoformat(formal["dt"].replace("T", " ")).replace(tzinfo=LONDON)
    end = start + timedelta(hours=EVENT_HOURS)
    location = formal["location"] or f"{formal['host_college']} College, Cambridge"
    desc_parts = []
    if formal["price"]:
        desc_parts.append(f"Price: {formal['price']}")
    if formal["instructions"]:
        desc_parts.append(formal["instructions"])
    desc_parts.append(f"Organised via {config.SITE_URL}")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Pembroke Formal Swaps//EN",
        "METHOD:PUBLISH",
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
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"
