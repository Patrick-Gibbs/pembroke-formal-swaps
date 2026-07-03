"""Admin CLI:  venv/bin/python tools.py <command>

  gen-secrets            print fresh SECRET_KEY and PIN_PEPPER lines for .env
  hash-admin-password    prompt for a password, print ADMIN_PASSWORD_HASH line
  init-db                create/upgrade the database schema
"""
import getpass
import secrets
import sys

sys.path.insert(0, ".")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "gen-secrets":
        print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
        print(f"PIN_PEPPER={secrets.token_urlsafe(32)}")
    elif cmd == "hash-admin-password":
        from swaps.security import hash_secret
        pw = getpass.getpass("New admin password: ")
        if len(pw) < 12:
            sys.exit("Refusing: use at least 12 characters.")
        if pw != getpass.getpass("Repeat: "):
            sys.exit("Passwords differ.")
        print("Put this line in .env (replacing the old one):")
        print(f"ADMIN_PASSWORD_HASH={hash_secret(pw)}")
    elif cmd == "init-db":
        from swaps.db import init_db
        init_db()
        print("Database ready.")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
