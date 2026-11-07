"""Create the app's tables and add the local demo logins.

Idempotent and non-destructive: it only creates missing tables and missing users. It never
drops anything, so reviewer bad-parse flags (`listing_flags`) survive a re-run.

    python scripts/seed_users.py

The DB defaults to ./trident.db (SQLite); point DATABASE_URL elsewhere to override.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import models  # noqa: E402,F401  (registers the tables on Base.metadata)
from app.auth import hash_password  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import User, UserRole  # noqa: E402

# Local-only fixed demo credentials, not secrets. Do not reuse them anywhere, and do not
# expose this app beyond localhost with these defaults intact.
DEMO_USERS = [
    ("viewer", "viewer123", UserRole.VIEWER),
    ("underwriter", "underwriter123", UserRole.UNDERWRITER),
    ("admin", "admin123", UserRole.ADMIN),
]


def main() -> None:
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        existing = {u for (u,) in session.query(User.username).all()}
        added = []
        for username, password, role in DEMO_USERS:
            if username not in existing:
                session.add(User(username=username, password_hash=hash_password(password), role=role))
                added.append((username, password, role))
        session.commit()
    finally:
        session.close()

    if added:
        print("Added local demo users (not secrets):")
        for username, password, role in added:
            print(f"  {username:12s} / {password:16s} ({role.value})")
    else:
        print("Demo users already present; nothing to do.")


if __name__ == "__main__":
    main()
