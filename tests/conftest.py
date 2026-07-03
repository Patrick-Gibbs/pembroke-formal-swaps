import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SWAPS_ENV_FILE", "/nonexistent")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("PIN_PEPPER", "test-pepper")
os.environ.setdefault("EMAIL_MODE", "dev")

import pytest

from swaps.db import connect, init_db


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    conn = connect(path)
    yield conn
    conn.close()


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def add_user(conn, uid, email=None):
    conn.execute(
        "INSERT INTO users(id, first_name, last_name, email, email_verified, pin_hash) "
        "VALUES (?,?,?,?,1,'x')", (uid, f"U{uid}", "Test", email or f"u{uid}@pem.cam.ac.uk"))


def add_formal(conn, fid, slots, dt="2030-01-01 19:30", term="T1"):
    conn.execute(
        "INSERT INTO formals(id, host_college, dt, price, slots, term, ballot_open, "
        "ballot_close, status) VALUES (?,?,?,?,?,?,?,?,'allocated')",
        (fid, f"College{fid}", dt, "", slots, term, "2029-01-01 09:00", "2029-12-01 09:00"))
