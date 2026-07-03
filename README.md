# Pembroke Formal Swaps

Web app for organising Pembroke College formal swaps: members register with a
`cam.ac.uk` email, rank the formals they want, a seeded random ballot allocates
places fairly, and hosts get attendee + dietary lists. Cancellation releases the
seat at a random moment within an hour and emails subscribers a claim link.

**Stack:** Python 3.14 · Flask · SQLite (stdlib `sqlite3`, WAL) · argon2id ·
waitress (localhost:8000) · Caddy (TLS) · Resend (email). Flask was chosen over
FastAPI to avoid compiled deps (`pydantic-core`) on the new interpreter; every
dependency installed clean on 3.14.

## Layout on this VM

```
/opt/swaps/
├── app/            # this repo (git; root-owned, group swaps read-only)
│   ├── swaps/      # Flask package (views/, templates/, allocation.py, ...)
│   ├── tests/      # pytest suite
│   ├── wsgi.py     # entrypoint (waitress + release-worker thread)
│   ├── tools.py    # CLI: gen-secrets | hash-admin-password | init-db
│   └── schema.sql
├── venv/           # Python 3.14 virtualenv
├── data/swaps.db   # SQLite database (owned by user `swaps`)
├── backups/        # nightly gzipped copies, last 7 kept
├── backup.sh
└── .env            # secrets — root:swaps 640, NEVER committed
/etc/systemd/system/swaps.service   # runs app as user `swaps`
/etc/caddy/Caddyfile                # TLS + reverse proxy
/etc/cron.d/swaps-backup            # nightly 03:12 UTC
```

## Environment variables (`/opt/swaps/.env`)

See `.env.example` for the full list. The important ones:

| Var | Meaning |
|---|---|
| `SECRET_KEY` | session/token signing key (`tools.py gen-secrets`) |
| `PIN_PEPPER` | server-side pepper mixed into PIN hashes — **changing it invalidates all PINs** |
| `ADMIN_PASSWORD_HASH` | argon2id hash (`tools.py hash-admin-password`) |
| `RESEND_API_KEY` | Resend API key — **paste the real value yourself** |
| `MAIL_FROM` | `noreply@pembrokeformalswaps.com` |
| `EMAIL_MODE` | `dev` (log emails to journal) / `live` (send via Resend) |
| `SITE_URL` | base URL used in emailed links |

### ➜ The one manual step left for you

```bash
sudo nano /opt/swaps/.env       # set RESEND_API_KEY=<real key>, EMAIL_MODE=live
sudo systemctl restart swaps
```

Real values already generated and in place: `SECRET_KEY`, `PIN_PEPPER`,
`ADMIN_PASSWORD_HASH`. The admin password itself is in
`/opt/swaps/ADMIN_PASSWORD.txt` (root-only) — **read it, store it in a password
manager, then delete the file.** Rotate any time with
`cd /opt/swaps/app && ../venv/bin/python tools.py hash-admin-password`.

## Operating it

```bash
systemctl status swaps          # app service (auto-restarts, starts on boot)
journalctl -u swaps -f          # logs — dev-mode emails appear here
systemctl reload caddy          # after editing /etc/caddy/Caddyfile
cd /opt/swaps/app && ../venv/bin/python -m pytest tests/   # run tests (as root)
```

Run locally / on a laptop: create a venv, `pip install flask waitress
argon2-cffi requests pytest`, copy `.env.example` → `.env`, fill it in, then
`COOKIE_SECURE=0 SITE_URL=http://127.0.0.1:8000 python wsgi.py` and browse to
http://127.0.0.1:8000 (COOKIE_SECURE=0 only for plain-HTTP local testing).

## Domain / DNS status

Caddy is configured for `pembrokeformalswaps.com` + `www` and will provision
Let's Encrypt certificates **automatically, no action on the VM needed** — but
as of deployment the domain (delegated to Cloudflare: `brodie`/`wally
.ns.cloudflare.com`) has **no A record published**. In the Cloudflare DNS
dashboard add, as **DNS-only (grey cloud, not proxied)** records:

```
A    pembrokeformalswaps.com      77.68.16.116
A    www.pembrokeformalswaps.com  77.68.16.116
```

Caddy retries every minute; within ~2 minutes of the records appearing,
https://pembrokeformalswaps.com goes live. Until then the site is served at
**https://77.68.16.116** with a self-signed cert (browser warning is expected);
that fallback block in the Caddyfile can be deleted afterwards.

## Allocation policy (as implemented)

1. **Rounds:** everyone gets at most one place before anyone gets a second.
   Round 2 starts only when round 1 can assign nothing more.
2. **Within a round:** every applicant bids on their highest-ranked formal that
   still has seats (and that they don't already hold). All simultaneous bids on
   a formal with enough seats succeed; oversubscribed formals draw winners
   **uniformly at random** from a seeded RNG. Losers automatically fall through
   to their next open choice in the same round.
3. **Empty-handed case:** you get nothing only if every formal you ranked
   filled up — i.e. you had no satisfiable preference left. Seconds can then
   still be handed out (this matches the agreed fairness exception).
4. **Reproducibility:** every run records its seed (Admin → Allocation runs,
   with a log of every lottery). Re-running with the same seed on unchanged
   data gives byte-identical results. Re-running after edits only fills seats,
   never revokes: existing active places are always kept.

Edge cases to be aware of (flagged for confirmation): a re-run after new
sign-ups can give an existing holder a *second* place only via round 2+, so
newcomers with no place are still served first; manual admin assignments count
as held places in later runs; nothing prevents one person holding places at two
formals on the same evening — the admin roster is the place to catch that.

## How a term works (admin runbook)

1. Log in at `/admin/login`. Set **current term** (e.g. `Michaelmas 2026`) in
   settings; choose whether the attendee list is public or login-only.
2. Create formals: college, date/time (UK time), price, **slots**, term, and
   the ballot open/close window (shared window per term is the normal case).
3. Members register (`cam.ac.uk` email + emailed verification link + 4-digit
   PIN) and rank formals at `/rank` while the window is open.
4. **Run allocation** (Admin → Run allocation). Leave seed blank for a fresh
   random one — it's recorded. Formals flip to `allocated`; ranking closes.
5. Attendee lists appear at `/attendees`; per-formal **CSV export** (names +
   dietary) is on each admin roster page for the host college.
6. Cancellations (self-service until 24 h before; admin removals any time)
   create a hidden hold that opens at a random point within an hour; the
   background worker then emails that formal's subscribers a claim link.
   Claims are atomic — the last seat can only go to one person.

## Backups & restore

Nightly at 03:12 UTC, `/opt/swaps/backup.sh` takes a consistent online copy
(`sqlite3 .backup`, WAL-safe) into `/opt/swaps/backups/`, keeping the newest 7.
Run manually: `sudo -u swaps /opt/swaps/backup.sh`.

**Restore:**
```bash
sudo systemctl stop swaps
gunzip -c /opt/swaps/backups/swaps-YYYYMMDD-HHMMSS.db.gz | sudo -u swaps tee /opt/swaps/data/swaps.db > /dev/null
sudo rm -f /opt/swaps/data/swaps.db-wal /opt/swaps/data/swaps.db-shm
sudo systemctl start swaps
```

## Security notes

- PINs: argon2id with per-user salt **plus** the `.env` pepper; never logged.
  Users are warned a 4-digit PIN is low-security.
- Login throttling: 5 consecutive failures → 15 min lock, doubling per further
  failure (cap 24 h); plus 20 failures/hour per IP. Applies to admin too.
- Sessions: signed cookies, `HttpOnly` + `Secure` + `SameSite=Lax`. CSRF token
  required on every POST. All SQL is parameterised.
- Admin uses a long argon2id-hashed password (no PIN), separate rate-limit
  bucket, and every admin action lands in the audit log.
- App runs as the unprivileged `swaps` user under a hardened systemd unit
  (`ProtectSystem=strict`, only `/opt/swaps/data` writable).
