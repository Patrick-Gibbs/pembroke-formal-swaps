"""Configuration loaded from a .env file (simple KEY=VALUE lines)."""
import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_DIR = os.path.dirname(APP_DIR)  # /opt/swaps


def load_env(path=None):
    path = path or os.environ.get("SWAPS_ENV_FILE", os.path.join(BASE_DIR, ".env"))
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


load_env()

SECRET_KEY = os.environ.get("SECRET_KEY", "")
PIN_PEPPER = os.environ.get("PIN_PEPPER", "")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "noreply@pembrokeformalswaps.com")
EMAIL_MODE = os.environ.get("EMAIL_MODE", "dev")  # 'live' | 'dev'
SITE_URL = os.environ.get("SITE_URL", "https://pembrokeformalswaps.com")
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "swaps.db"))
PHOTOS_DIR = os.environ.get("PHOTOS_DIR",
                            os.path.join(os.path.dirname(DB_PATH), "photos"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8000"))
# Behind Caddy we trust X-Forwarded-For for rate limiting.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "1") == "1"
# Set 0 only for plain-HTTP local testing; must be 1 in production.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") == "1"

CANCEL_CUTOFF_HOURS = 72  # fallback; live value is the cancel_cutoff_hours setting
VERIFY_TOKEN_MAX_AGE = 24 * 3600
TIMEZONE = "Europe/London"
