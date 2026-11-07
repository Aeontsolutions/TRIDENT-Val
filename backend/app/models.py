import enum

from sqlalchemy import (JSON, BigInteger, Column, DateTime, Enum, Integer, String, Text)
from sqlalchemy.sql import func

from .db import Base


class SystemLog(Base):
    """Unified operational + audit log, fed by loguru (backend) and loglevel (frontend).

    Doubles as the compliance audit trail (e.g. underwriter overrides) and as
    system telemetry (request latency, model inference stats, client errors).
    """
    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    source = Column(String(16), index=True)     # 'backend' | 'frontend'
    level = Column(String(10), index=True)       # DEBUG / INFO / WARNING / ERROR
    logger_name = Column(String(64))              # e.g. 'api.audit', 'frontend.inspector'
    message = Column(Text, nullable=False)        # human-readable event description
    pid = Column(BigInteger, index=True, nullable=True)  # related property, if any
    context = Column(JSON)                         # structured payload (deltas, latency, etc.)
    actor = Column(String(64), index=True, nullable=True)  # authenticated username; NULL if none


class UserRole(str, enum.Enum):
    VIEWER = "Viewer"
    UNDERWRITER = "Underwriter"
    ADMIN = "Admin"


class User(Base):
    """PoC-grade user store: a handful of demo accounts (see scripts/seed_db.py),
    not a production identity system. Roles are re-checked from the DB on every
    request (see app.auth.get_current_user) rather than trusted from the session,
    so a role change takes effect on the user's very next request."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(128), nullable=False)  # bcrypt hash
    role = Column(Enum(UserRole, values_callable=lambda e: [m.value for m in e]),
                  default=UserRole.VIEWER, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ListingFlag(Base):
    """A reviewer's "this field was parsed wrong" note on a scraped listing observation.

    The ONLY write path of the Listings explorer, and it lives in the app DB: nothing is ever
    written back to BigQuery/GCS. `record_id` is the observation id (per scrape run), so
    `site_listing_ref` is kept alongside to find the same listing in other runs. Raw/parsed
    values are snapshotted at flag time so the flag stays meaningful if the table is reloaded.
    Exported as CSV so open flags can be turned into parser fixes.
    """
    __tablename__ = "listing_flags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    record_id = Column(String(128), index=True, nullable=False)
    site_listing_ref = Column(String(128), index=True, nullable=True)
    scrape_run_id = Column(String(128), nullable=True)
    field = Column(String(64), index=True, nullable=False)     # e.g. "Floor area"
    reason = Column(String(32), index=True, nullable=False)    # see listing_flags.REASONS
    note = Column(Text, default="")
    expected_value = Column(String(256), nullable=True)        # what the parser should have produced
    raw_value = Column(Text, nullable=True)                    # snapshot: raw scraped text
    parsed_value = Column(Text, nullable=True)                 # snapshot: parser output
    status = Column(String(16), default="open", index=True, nullable=False)  # open | resolved
    created_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    resolved_by = Column(String(64), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolution_note = Column(Text, nullable=True)
