"""SQLite helpers. Parameterised queries only; BEGIN IMMEDIATE for writes
that must be atomic (slot claims, allocation runs)."""
import os
import sqlite3
from contextlib import contextmanager

from flask import g

from . import config

SCHEMA_PATH = os.path.join(config.APP_DIR, "schema.sql")


def connect(db_path=None):
    conn = sqlite3.connect(db_path or config.DB_PATH, timeout=15,
                           isolation_level=None)  # autocommit; explicit BEGIN when needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(db_path=None):
    path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = connect(path)
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    # Columns added after first deployment (CREATE IF NOT EXISTS won't add them).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(formals)")}
    for col, ddl in [("location", "TEXT NOT NULL DEFAULT ''"),
                     ("instructions", "TEXT NOT NULL DEFAULT ''"),
                     ("reminder_sent", "INTEGER NOT NULL DEFAULT 0"),
                     ("review_sent", "INTEGER NOT NULL DEFAULT 0"),
                     ("host_name", "TEXT NOT NULL DEFAULT ''"),
                     ("host_email", "TEXT NOT NULL DEFAULT ''"),
                     ("host_phone", "TEXT NOT NULL DEFAULT ''"),
                     ("endowment_m", "REAL")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE formals ADD COLUMN {col} {ddl}")
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(allocations)")}
    if "notified" not in acols:
        conn.execute("ALTER TABLE allocations ADD COLUMN notified "
                     "INTEGER NOT NULL DEFAULT 0")
        # Pre-existing allocations were emailed under the old flow.
        conn.execute("UPDATE allocations SET notified=1")
    conn.close()


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def immediate(conn):
    """Exclusive-writer transaction; rolls back on error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def get_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def audit(conn, actor, action, detail=""):
    conn.execute("INSERT INTO audit_log(actor, action, detail) VALUES (?,?,?)",
                 (actor, action, detail))
