# Formal Swaps
A complete, self-hosted web app for running **formal swaps** — organising your
college's members to attend formal dinners at other colleges, and tracking the
other colleges that visit you. Built for Pembroke College, Cambridge
(live at https://pembrokeformalswaps.com) but designed so any college MCR/JCR
can deploy their own copy on a cheap VM.

**Stack:** Python 3 (3.12+; runs on 3.14) · Flask · SQLite (stdlib, WAL mode)
· argon2id password hashing · waitress WSGI server · Caddy (automatic HTTPS)
· [Resend](https://resend.com) for all outbound email. No Postgres, no Redis,
no Node build chain — one small process plus SQLite, comfortable on a 1 vCPU /
2 GB VM.

Created by Patrick Gibbs. Free to reuse under the
[MIT license with attribution](LICENSE): any public site built on this code
must credit "Patrick Gibbs" and link to
[this repository](https://github.com/Patrick-Gibbs/pembroke-formal-swaps)
somewhere discoverable (e.g. the footer).

---

## 1. Functionality

### For members

- **Registration** with first/last name, dietary requirements (tick-boxes +
  free text), a **4-digit PIN** (argon2id-hashed with a server pepper; users
  are warned it's low-security), and a university email address
  (`*.cam.ac.uk` by default — see [Customising](#7-customising-for-your-college))
  verified via a signed 24-hour email link.
- **PIN reset** by email link (`/forgot-pin`, 1-hour signed token).
- **Ranking** (`/rank`): drag-style ordered list of the term's formals —
  rank only the ones you want; editable until the ballot closes. A live
  **countdown** to the shared close time shows on the homepage and rank page.
- **Max-places cap**: each member sets "maximum number of swaps I'm happy to
  be assigned if there is capacity" (1–3). The ballot never assigns more,
  though members can still claim extra freed places themselves later.
- **Group balloting** (`/ballot/`): form a group (cap 4), invite registered
  members by name with live autocomplete; invitees accept/decline on-site or
  via the emailed link. The group enters the ballot as one block with the
  leader's ranking — all seated together or not at all. One group per member
  per term; the leader's max-places cap applies to the group.
- **Results by email** on publish: each winner gets all their formals with
  **.ics calendar invitations attached** (one tap adds the event, with
  location, instructions, price, and a 2-hour-before alarm); entrants who got
  nothing get a courteous miss note.
- **Assigned swaps page** (`/attendees`): public (or login-only — switchable)
  list of who's attending each formal, in date order, with per-formal dietary
  summaries for host colleges. Hidden until the admin publishes.
- **Cancellation**: self-service until a configurable cut-off (default **72
  hours**) before the formal. A freed place is **not** released instantly —
  it opens at a random moment within the next hour (so it can't be handed
  straight to a friend), then all subscribers are emailed a claim link.
- **Slot alerts**: "Notify me" on any formal emails you when a place opens.
  **Claiming is atomic** — two people can't both take the last seat.
- **Day-of emails**: 9:00 UK reminder (with details, calendar file, and a
  nudge to review afterwards); 19:30 review request.
- **Reviews** (`/reviews`, public): rate a formal out of 5 stars — one per
  good course (3, shown gold) plus hosts & atmosphere (2, shown silver) — with
  optional text and photo; reviews open from 9:00 on the day. The page shows
  every review, each college's **mean ± standard deviation**, and two
  live-updating charts: average rating by college (± standard error, ordered
  worst→best) and average rating vs college endowment (log₁₀ scale).
- **Incoming swaps** (`/incoming`): public schedule of colleges visiting you,
  with guest names only.

### For the admin

Separate login (`/admin/login`) with a **long argon2id-hashed password** (not
a PIN), its own rate-limit bucket, and a full audit log.

- **Formals CRUD**: host college, date/time, price, slots, term, location and
  instructions (flow into calendar invites and reminder emails), internal host
  contact (name/email/phone — never shown to members), host college endowment
  (£m — autofills from a seeded table of Cambridge colleges, editable, feeds
  the reviews charts), and optional per-formal ballot window override.
- **Settings**: current term, term-wide **ballot open/close** (the close time
  is public with a countdown), attendee-list visibility, **cancellation
  cut-off hours**.
- **Generate → Preview → Publish workflow**:
  - *Generate* runs the seeded ballot as a **draft** — nothing emailed,
    nothing public. Re-generate freely (with a recorded or chosen seed).
  - *Preview* (`/admin/preview`) shows every formal's roster on one page,
    with fill counts, "(new)" markers on unannounced places, and a list of
    entrants who got nothing — the pre-publish sanity check.
  - *Publish* emails every unannounced winner (+ calendar invites) and, on
    first publish, the miss notes; the assigned-swaps page goes public.
    Publishing twice never duplicates emails; **Republish** deliberately
    re-sends everything for the term.
- **Rosters**: per-formal attendee list with dietary + contact email, manual
  add/remove (bypasses the cut-off), CSV export, **📋 copy-for-email button**
  (pastes as a formatted table into Gmail/Outlook/Word, or tab-separated into
  Excel — for catering/finance), and **email-all-attendees** (custom
  subject/body to everyone on a formal).
- **Export all**: one CSV of every roster (dashboard) and a dashboard
  **copy-all-rosters** button producing every formal's formatted table in one
  clipboard copy.
- **Incoming swaps admin** (`/admin/incoming`): create/edit each visiting
  college's dinner with **host organiser contact details** (name, email,
  phone, notes — never public), an inline-editable guest list (name, dietary,
  notes per row), per-swap copy-for-email button, and export-all CSV.
- **Allocation runs log**: every run with its seed and every lottery drawn —
  same seed + same data reproduces the identical result.
- **Users list**, **subscriptions & releases view**, **audit log** (all admin
  actions + cancels/claims), and an **email log** (`/admin/emails`) recording
  every send attempt with success/failure and a 7-day failure summary.

### Automated background behaviour (no cron needed beyond backups)

A worker thread inside the app process, every 30 s:

- opens due released slots and emails that formal's subscribers;
- sends the 9:00 day-of reminders (skipped if the formal already started);
- sends the 19:30 review requests;
- all exactly-once, guarded by DB transactions.

### Allocation algorithm (the important bit)

A **preference-honouring random ballot with fairness rounds**:

1. Allocation runs in **rounds**: nobody receives a 2nd formal while any
   unit (solo or group) that still has a satisfiable preference has none.
2. Within a round, every unit bids on its highest-ranked formal that still
   has enough free seats (group = block of `size` seats). All bids at a step
   resolve together — everyone's 1st choice is considered before anyone's
   2nd.
3. Oversubscribed formals draw winners **uniformly at random from a seeded
   RNG**; losers cascade to their next viable choice in the same round.
   Groups that don't fit are passed over; leftover seats stay winnable by
   smaller units.
4. You end up with nothing only if everything you ranked filled up (or never
   had enough adjacent seats for your group).
5. Personal caps (1–3) limit how many formals the ballot hands a unit;
   groups use the leader's cap. Caps apply to the ballot only.
6. Every run records its **seed** and a log of every lottery; re-running with
   the same seed and data is byte-identical. Re-runs only fill seats — they
   never revoke existing places.

Tested by 31 automated tests including randomised invariant checks
(capacity, round fairness, determinism, group blocks, caps), rate-limiter
lockout behaviour, threaded races on the last seat, ICS timezone handling
(GMT/BST), and the scheduled-email windows.

---

## 2. Repository layout

```
app/
├── swaps/                  # Flask package
│   ├── __init__.py         # app factory (CSRF, sessions, blueprints)
│   ├── config.py           # .env loading + constants
│   ├── db.py               # SQLite helpers, schema migrations, BEGIN IMMEDIATE
│   ├── security.py         # argon2id+pepper, CSRF, rate limiting, decorators
│   ├── allocation.py       # pure seeded ballot algorithm (no DB — testable)
│   ├── services.py         # ballot runs, cancel/claim, scheduled emails
│   ├── emailer.py          # Resend client + every email template + email_log
│   ├── ics.py              # iCalendar invite generation (Europe/London aware)
│   ├── release_worker.py   # 30-second background worker thread
│   ├── views/              # auth.py, main.py, ballot.py, admin.py
│   └── templates/          # server-rendered Jinja2 (no build step)
├── tests/                  # pytest suite (31 tests)
├── schema.sql              # full schema; applied idempotently at startup
├── wsgi.py                 # entrypoint: waitress + worker thread
├── tools.py                # CLI: gen-secrets | hash-admin-password | init-db
├── .env.example            # every env var, with placeholders
└── README.md
```

Data lives *outside* the repo: SQLite DB + uploaded review photos in a `data/`
directory, secrets in `.env` — neither is ever committed.

---

## 3. What you need before you start

| Requirement | Why | Notes |
|---|---|---|
| **VM with a persistent public IPv4** | Hosts the site; Let's Encrypt and your DNS record need a stable address | Any cheap cloud box works: 1 vCPU, 2 GB RAM (add 2 GB swap), ~10 GB disk. Ubuntu 22.04+ assumed below. Ensure the provider gives a *static* IP or reserve one. |
| **A domain name** | HTTPS + a sending identity for email | ~£10/yr from any registrar. You need control of its DNS records. |
| **A [Resend](https://resend.com) account** | All outbound email (verification, results, reminders…) | Free tier is fine to start (check current limits against your member count — a publish emails everyone at once). |
| **Ability to SSH as root** (or sudo) | Setup | Everything below is copy-paste shell. |

---

## 4. Full setup, step by step

### 4.1 Point your domain at the VM

At your DNS provider create two **A records** (if you use Cloudflare, set
them to **DNS-only / grey cloud**, *not* proxied — Caddy must obtain its own
certificates):

```
A    yourdomain.com       <your VM's IPv4>
A    www.yourdomain.com   <your VM's IPv4>
```

### 4.2 Set up Resend (email)

1. Create a Resend account → **Domains → Add domain** → `yourdomain.com`.
2. Resend shows you DNS records to add (SPF TXT, DKIM CNAME/TXT records —
   and add a DMARC TXT record too, e.g. `v=DMARC1; p=none`). Add them at
   your DNS provider and wait for Resend to show **Verified**.
3. **API Keys → Create** — copy the `re_...` key somewhere safe. You'll
   paste it into `.env` in step 4.5. Your from-address will be
   `noreply@yourdomain.com`.

Until the domain verifies you can still run the whole site with
`EMAIL_MODE=dev` (emails print to the server log instead of sending).

### 4.3 Prepare the VM

```bash
# as root
apt update && apt install -y python3 python3-venv git ufw
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw enable

# Caddy (reverse proxy + automatic HTTPS) — official repo:
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy

# dedicated non-root user that owns the data, plus the directory layout
useradd --system --create-home --home-dir /opt/swaps --shell /usr/sbin/nologin swaps
mkdir -p /opt/swaps/{app,data,backups}
```

### 4.4 Install the app

```bash
cd /opt/swaps
git clone <this-repo-url> app        # or scp/rsync the app/ directory
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install flask waitress argon2-cffi requests pytest
```

> Python note: everything is pure-Python except `argon2-cffi`, which ships
> wheels for all current interpreters. If a brand-new Python ever lacks a
> wheel, create the venv with an older interpreter (e.g. 3.12 via deadsnakes).

### 4.5 Configure secrets (`/opt/swaps/.env`)

```bash
cd /opt/swaps/app
cp .env.example /opt/swaps/.env
../venv/bin/python tools.py gen-secrets          # prints SECRET_KEY + PIN_PEPPER lines
../venv/bin/python tools.py hash-admin-password  # prompts; prints ADMIN_PASSWORD_HASH line
```

Edit `/opt/swaps/.env` and fill in every value:

| Var | Value |
|---|---|
| `SECRET_KEY` | from `gen-secrets` — signs sessions and email tokens |
| `PIN_PEPPER` | from `gen-secrets` — mixed into every PIN hash. **Changing it later invalidates every member's PIN.** |
| `ADMIN_PASSWORD_HASH` | from `hash-admin-password` (12+ chars; store the password in a password manager) |
| `RESEND_API_KEY` | your `re_...` key |
| `MAIL_FROM` | `noreply@yourdomain.com` |
| `EMAIL_MODE` | `dev` while testing; **`live`** for production |
| `SITE_URL` | `https://yourdomain.com` (used in every emailed link) |
| `DB_PATH` | `/opt/swaps/data/swaps.db` |
| `LISTEN_PORT` | `8000` |
| `TRUST_PROXY` | `1` (behind Caddy) |
| `COOKIE_SECURE` | `1` (only ever `0` for plain-HTTP local testing) |

Lock it down: `chown root:swaps /opt/swaps/.env && chmod 640 /opt/swaps/.env`

### 4.6 systemd service

Create `/etc/systemd/system/swaps.service`:

```ini
[Unit]
Description=Formal Swaps web app
After=network.target

[Service]
Type=simple
User=swaps
Group=swaps
WorkingDirectory=/opt/swaps/app
ExecStart=/opt/swaps/venv/bin/python /opt/swaps/app/wsgi.py
Restart=always
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/opt/swaps/data
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
MemoryMax=512M

[Install]
WantedBy=multi-user.target
```

Then:

```bash
chown -R root:swaps /opt/swaps/app /opt/swaps/venv
chmod -R g+rX,o-rwx /opt/swaps/app
chown -R swaps:swaps /opt/swaps/data /opt/swaps/backups
systemctl daemon-reload
systemctl enable --now swaps
systemctl status swaps          # should be active; DB is created on first start
```

### 4.7 Caddy (HTTPS)

Replace `/etc/caddy/Caddyfile` with:

```
yourdomain.com {
	reverse_proxy 127.0.0.1:8000
	encode gzip
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "strict-origin-when-cross-origin"
	}
}

www.yourdomain.com {
	redir https://yourdomain.com{uri} permanent
}
```

`systemctl reload caddy` — within a couple of minutes Caddy obtains Let's
Encrypt certificates automatically (and renews them forever). If it can't,
your A records aren't resolving yet: `journalctl -u caddy | grep -i acme`.

### 4.8 Nightly backups

`/opt/swaps/backup.sh` (make executable, `chown root:swaps`, `chmod 750`):

```bash
#!/bin/bash
set -eu
DB=/opt/swaps/data/swaps.db
DEST=/opt/swaps/backups
STAMP=$(date -u +%Y%m%d-%H%M%S)
[ -f "$DB" ] || exit 0
/opt/swaps/venv/bin/python - "$DB" "$DEST/swaps-$STAMP.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst)   # WAL-safe online copy
dst.close(); src.close()
PY
gzip "$DEST/swaps-$STAMP.db"
ls -1t "$DEST"/swaps-*.db.gz 2>/dev/null | tail -n +8 | xargs -r rm --
```

Cron (`/etc/cron.d/swaps-backup`) — nightly, keep last 7:

```
12 3 * * * swaps /opt/swaps/backup.sh >> /opt/swaps/backups/backup.log 2>&1
```

**Restore:**

```bash
systemctl stop swaps
gunzip -c /opt/swaps/backups/swaps-YYYYMMDD-HHMMSS.db.gz \
  | sudo -u swaps tee /opt/swaps/data/swaps.db > /dev/null
rm -f /opt/swaps/data/swaps.db-wal /opt/swaps/data/swaps.db-shm
systemctl start swaps
```

(Also back up `/opt/swaps/data/photos/` if you care about review photos.)

### 4.9 First run

1. Visit `https://yourdomain.com/admin/login`, log in with your admin
   password.
2. **Settings**: set the current term (e.g. `Michaelmas 2026`), the term-wide
   ballot open/close times, cancellation cut-off (default 72 h), and whether
   the attendee list is public.
3. Create the term's **formals**: college, date/time (local UK time), price,
   slots, and — important for the calendar invites — **location** and
   **instructions** (dress code, meeting point, payment).
4. Test the member journey end-to-end with `EMAIL_MODE=dev` first if you
   like (emails appear in `journalctl -u swaps -f`), then set
   `EMAIL_MODE=live` in `.env` and `systemctl restart swaps`.
5. Register yourself as a member with your real address and confirm the
   verification email arrives (check `/admin/emails` for the send log).

---

## 5. Running a term (admin runbook)

1. Members register, verify, form groups, rank formals, set their max-places
   cap. The homepage shows a live countdown to the ballot close.
2. After the close: **Generate allocation** (blank seed = fresh random,
   recorded; or enter a seed to reproduce). Result is a **draft**.
3. **Preview rosters** — one page with every formal, fill counts, and
   everyone who got nothing. Re-generate if needed.
4. **Publish** — everyone gets their result email with calendar invites;
   the assigned swaps page goes public. (Later: **Republish** re-sends all.)
5. The app handles the rest automatically: cancellations + randomized slot
   release + subscriber alerts + atomic claims, 9am day-of reminders, 9pm
   review requests.
6. Before each formal: roster page → **Copy for email** → send to the host
   college's catering (dietary table) / your finance (billing emails).
7. Track colleges visiting you under **Incoming swaps** (host contact info,
   editable guest list with dietaries; public schedule shows names only).

---

## 6. Local development

```bash
python3 -m venv venv && venv/bin/pip install flask waitress argon2-cffi requests pytest
cp .env.example .env          # fill in gen-secrets values; EMAIL_MODE=dev
COOKIE_SECURE=0 SITE_URL=http://127.0.0.1:8000 venv/bin/python wsgi.py
# browse http://127.0.0.1:8000 — “sent” emails print to the console
venv/bin/python -m pytest tests/          # run the test suite
```

---

## 7. Customising for your college

All in-repo, grep-able changes:

- **Names/branding**: "Pembroke Formal Swaps" appears in
  `swaps/templates/base.html` (header, footer, `<title>`), `index.html`
  (hero + description), and the email subjects/bodies in `swaps/emailer.py`.
- **Allowed email domain**: `EMAIL_RE` in `swaps/views/auth.py` (default
  `*.cam.ac.uk`, any subdomain) plus the `pattern=` attribute and hint text
  in `templates/register.html`. Change for another university.
- **Calendar invite branding**: `PRODID`/UID host in `swaps/ics.py`.
- **Dietary tick-boxes**: `DIETARY_CHOICES` in `swaps/views/auth.py`.
- **Group size cap**: `MAX_GROUP_SIZE` in `swaps/views/ballot.py` (default 4).
- **Max-places range**: rank form in `templates/rank.html` + clamp in
  `swaps/views/main.py` (default 1–3).
- **Formal length in invites**: `EVENT_HOURS` in `swaps/ics.py` (default 3).
- **Timezone**: `TIMEZONE` in `swaps/config.py` (`Europe/London`).
- **Reminder/review send times** (9:00/21:00): `send_scheduled_emails` in
  `swaps/services.py`.

Everything else (term, ballot window, cut-off, list visibility) is runtime
admin configuration — no code edits.

---

## 8. Security model

- **PINs**: 4 digits, argon2id per-user salt + server-side pepper from
  `.env`; never stored or logged in plaintext. Members are explicitly warned
  a PIN is low-security.
- **Rate limiting** (users *and* admin): 5 consecutive failures → 15-minute
  lock, doubling each further failure (cap 24 h), per account; plus 20
  failures/hour per IP. PIN-reset requests share the IP throttle.
- **Sessions**: signed cookies, `HttpOnly` + `Secure` + `SameSite=Lax`.
  **CSRF token on every POST.** All SQL parameterised.
- **Admin**: long password (argon2id), separate rate-limit bucket, every
  action audit-logged.
- **Atomicity**: seat claims and ballot runs use SQLite `BEGIN IMMEDIATE`
  transactions; a partial unique index prevents duplicate active seats.
- **Process**: runs as an unprivileged system user under a hardened systemd
  unit (`ProtectSystem=strict` — only the data directory is writable);
  Caddy terminates TLS and sets HSTS/nosniff/frame-deny headers.
- **Privacy**: emails and dietary details never appear on public pages
  (public pages show names only); host-college contact info is admin-only.

## 9. Troubleshooting

| Symptom | Check |
|---|---|
| No HTTPS / cert errors | `journalctl -u caddy \| grep -i acme` — usually DNS A records missing/proxied |
| Emails not arriving | `/admin/emails` (in-app log: was the send accepted?), then the Resend dashboard for bounces; confirm `EMAIL_MODE=live` and the Resend domain is Verified |
| Site down | `systemctl status swaps`, `journalctl -u swaps -n 50` |
| "Permission denied" on start after editing code | re-run the ownership commands from §4.6 (files must be group-readable by `swaps`) |
| Locked out of admin | wait out the lockout (it doubles per attempt), or rotate: `tools.py hash-admin-password` → update `.env` → restart |
| Verify a backup works | restore it to a scratch path and open with `sqlite3` |

---

## 10. License

MIT with one extra condition (see [LICENSE](LICENSE)): you're free to use,
modify, and run this for your own college, but any public site built on it
must **credit "Patrick Gibbs" and link to
[this repository](https://github.com/Patrick-Gibbs/pembroke-formal-swaps)**
somewhere discoverable (footer or about page is fine).

---

*Built with Flask + SQLite on purpose: one process, one file of state, no
external services beyond DNS + Resend. If your college outgrows it, the
allocation engine (`swaps/allocation.py`) is a pure function you can lift
into anything.*
