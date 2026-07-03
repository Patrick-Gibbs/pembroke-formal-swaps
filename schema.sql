PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email_verified INTEGER NOT NULL DEFAULT 0,
    pin_hash TEXT NOT NULL,
    dietary_flags TEXT NOT NULL DEFAULT '',   -- comma-separated ticked boxes
    dietary_other TEXT NOT NULL DEFAULT '',   -- free text
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS formals (
    id INTEGER PRIMARY KEY,
    host_college TEXT NOT NULL,
    dt TEXT NOT NULL,               -- 'YYYY-MM-DD HH:MM' Europe/London local time
    price TEXT NOT NULL DEFAULT '',
    slots INTEGER NOT NULL,
    term TEXT NOT NULL,
    ballot_open TEXT NOT NULL,      -- 'YYYY-MM-DD HH:MM' local
    ballot_close TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',  -- open | allocated | cancelled
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS preferences (
    user_id INTEGER NOT NULL REFERENCES users(id),
    formal_id INTEGER NOT NULL REFERENCES formals(id),
    rank INTEGER NOT NULL,
    term TEXT NOT NULL,
    UNIQUE(user_id, formal_id),
    UNIQUE(user_id, term, rank)
);

CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    formal_id INTEGER NOT NULL REFERENCES formals(id),
    status TEXT NOT NULL DEFAULT 'active',   -- active | cancelled
    source TEXT NOT NULL DEFAULT 'ballot',   -- ballot | claim | admin
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    cancelled_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alloc_active
    ON allocations(user_id, formal_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS released_slots (
    id INTEGER PRIMARY KEY,
    formal_id INTEGER NOT NULL REFERENCES formals(id),
    allocation_id INTEGER REFERENCES allocations(id),
    release_at TEXT NOT NULL,        -- UTC ISO; slot is hidden until then
    opened INTEGER NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0,
    claimed_by INTEGER REFERENCES users(id),
    claimed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    formal_id INTEGER NOT NULL REFERENCES formals(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, formal_id)
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- 'user' | 'admin'
    identifier TEXT NOT NULL,        -- lowercased email, or 'ip:<addr>'
    success INTEGER NOT NULL,
    at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_attempts ON login_attempts(kind, identifier, at);

CREATE TABLE IF NOT EXISTS allocation_runs (
    id INTEGER PRIMARY KEY,
    term TEXT NOT NULL,
    seed TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL DEFAULT (datetime('now')),
    actor TEXT NOT NULL,             -- 'admin' or user email
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ballot_groups (
    id INTEGER PRIMARY KEY,
    term TEXT NOT NULL,
    leader_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ballot_group_members (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES ballot_groups(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'invited',   -- invited | accepted | declined
    invited_at TEXT NOT NULL DEFAULT (datetime('now')),
    responded_at TEXT,
    UNIQUE(group_id, user_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO settings(key, value) VALUES ('attendee_list_public', '1');
INSERT OR IGNORE INTO settings(key, value) VALUES ('current_term', '');
