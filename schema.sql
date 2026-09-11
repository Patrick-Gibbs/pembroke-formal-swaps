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
    location TEXT NOT NULL DEFAULT '',      -- meeting point / address for calendar
    instructions TEXT NOT NULL DEFAULT '',  -- dress code, payment, arrival time...
    host_name TEXT NOT NULL DEFAULT '',     -- host college contact (admin-only)
    host_email TEXT NOT NULL DEFAULT '',
    host_phone TEXT NOT NULL DEFAULT '',
    endowment_m REAL,                       -- host college endowment £m (nullable)
    reminder_sent INTEGER NOT NULL DEFAULT 0,   -- 9am day-of email done
    review_sent INTEGER NOT NULL DEFAULT 0,     -- 9pm review request done
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
    notified INTEGER NOT NULL DEFAULT 0,     -- result email sent (at publish)
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

CREATE TABLE IF NOT EXISTS college_endowments (
    college TEXT PRIMARY KEY COLLATE NOCASE,
    endowment_m REAL NOT NULL          -- endowment in £ millions
);
-- Seed: Cambridge college endowments (£m), for autofill suggestions. Editable
-- by the admin (saving a formal upserts the value used).
INSERT OR IGNORE INTO college_endowments(college, endowment_m) VALUES
  ('Trinity', 2020), ('St John''s', 674), ('King''s', 340),
  ('Gonville & Caius', 271), ('Peterhouse', 238.6), ('Jesus', 236),
  ('Clare', 187.5), ('Emmanuel', 143), ('Pembroke', 139), ('Christ''s', 122),
  ('Queens''', 120), ('Homerton', 119), ('Corpus Christi', 100),
  ('Trinity Hall', 89), ('Fitzwilliam', 77), ('Newnham', 74),
  ('Magdalene', 74), ('St Catharine''s', 74), ('Girton', 73), ('Selwyn', 55),
  ('Murray Edwards', 54), ('Downing', 44), ('Churchill', 37), ('Wolfson', 32),
  ('Sidney Sussex', 31), ('Robinson', 30), ('Darwin', 25), ('Clare Hall', 21),
  ('St Edmund''s', 19), ('Lucy Cavendish', 14), ('Hughes Hall', 8);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    formal_id INTEGER NOT NULL REFERENCES formals(id),
    course_stars INTEGER NOT NULL,   -- 0-3, one per good course
    vibe_stars INTEGER NOT NULL,     -- 0-2, hosts + college
    review TEXT NOT NULL DEFAULT '',
    photo TEXT NOT NULL DEFAULT '',  -- filename under data/photos
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, formal_id)
);

CREATE TABLE IF NOT EXISTS ballot_caps (
    user_id INTEGER NOT NULL REFERENCES users(id),
    term TEXT NOT NULL,
    max_places INTEGER NOT NULL DEFAULT 3,   -- 1-3, "happy to be assigned"
    UNIQUE(user_id, term)
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

CREATE TABLE IF NOT EXISTS incoming_swaps (
    id INTEGER PRIMARY KEY,
    guest_college TEXT NOT NULL,
    dt TEXT NOT NULL,                    -- 'YYYY-MM-DD HH:MM' UK time
    host_name TEXT NOT NULL DEFAULT '',  -- their organiser
    host_email TEXT NOT NULL DEFAULT '',
    host_phone TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS incoming_participants (
    id INTEGER PRIMARY KEY,
    swap_id INTEGER NOT NULL REFERENCES incoming_swaps(id),
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    dietary TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS email_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL DEFAULT (datetime('now')),
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    ok INTEGER NOT NULL,             -- 1 = accepted by Resend (or dev mode)
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO settings(key, value) VALUES ('attendee_list_public', '1');
INSERT OR IGNORE INTO settings(key, value) VALUES ('current_term', '');
INSERT OR IGNORE INTO settings(key, value) VALUES ('results_published', '1');
INSERT OR IGNORE INTO settings(key, value) VALUES ('cancel_cutoff_hours', '72');
