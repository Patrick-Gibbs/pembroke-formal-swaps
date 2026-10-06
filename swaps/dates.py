"""UK-style display of the ISO date strings stored in the DB.

Storage stays 'YYYY-MM-DD HH:MM[:SS]' (sortable, what SQLite and
<input type=datetime-local> expect); everything shown to people goes
through these so it reads dd/mm/yyyy.
"""
from datetime import datetime


def _parse(s):
    return datetime.fromisoformat(str(s).strip().replace("T", " "))


def uk_date(s):
    """'2026-10-09 18:00' -> '09/10/2026'. Blank/unparseable values pass through."""
    if not s:
        return s
    try:
        return _parse(s).strftime("%d/%m/%Y")
    except ValueError:
        return s


def uk_datetime(s):
    """'2026-10-09 18:00[:SS]' -> '09/10/2026 18:00' (date-only input gives just
    the date). Blank/unparseable values pass through."""
    if not s:
        return s
    try:
        d = _parse(s)
    except ValueError:
        return s
    if len(str(s).strip()) <= 10:
        return d.strftime("%d/%m/%Y")
    return d.strftime("%d/%m/%Y %H:%M")


def from_uk(s):
    """Typed/imported date -> stored 'YYYY-MM-DD HH:MM'. Accepts dd/mm/yyyy
    [HH:MM] (UK) or ISO. '' stays ''; anything else raises ValueError."""
    s = (s or "").strip().replace("T", " ")
    if not s:
        return ""
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return datetime.fromisoformat(s).strftime("%Y-%m-%d %H:%M")
