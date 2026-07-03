from swaps.security import (ACCOUNT_THRESHOLD, IP_LIMIT_PER_HOUR, ip_blocked,
                            lockout_remaining, record_attempt)


def fail(db, n, email="a@pem.cam.ac.uk", ip="1.2.3.4"):
    for _ in range(n):
        record_attempt(db, "user", email, ip, success=False)


def test_no_lockout_below_threshold(db):
    fail(db, ACCOUNT_THRESHOLD - 1)
    assert lockout_remaining(db, "user", "a@pem.cam.ac.uk") == 0


def test_lockout_at_threshold_and_escalation(db):
    fail(db, ACCOUNT_THRESHOLD)
    base = lockout_remaining(db, "user", "a@pem.cam.ac.uk")
    assert 0 < base <= 15 * 60
    fail(db, 1)  # 6th consecutive failure doubles the lock
    assert lockout_remaining(db, "user", "a@pem.cam.ac.uk") > base


def test_success_resets_consecutive_count(db):
    fail(db, ACCOUNT_THRESHOLD)
    record_attempt(db, "user", "a@pem.cam.ac.uk", "1.2.3.4", success=True)
    assert lockout_remaining(db, "user", "a@pem.cam.ac.uk") == 0


def test_lockout_is_per_account(db):
    fail(db, ACCOUNT_THRESHOLD)
    assert lockout_remaining(db, "user", "other@pem.cam.ac.uk") == 0


def test_ip_block(db):
    for i in range(IP_LIMIT_PER_HOUR):
        record_attempt(db, "user", f"u{i}@pem.cam.ac.uk", "9.9.9.9", success=False)
    assert ip_blocked(db, "user", "9.9.9.9")
    assert not ip_blocked(db, "user", "8.8.8.8")


def test_admin_attempts_separate_from_user(db):
    fail(db, ACCOUNT_THRESHOLD, email="admin")
    assert lockout_remaining(db, "admin", "admin") == 0  # kind differs
