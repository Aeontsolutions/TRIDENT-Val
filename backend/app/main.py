import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()  # picks up a repo-root .env for local (non-Docker) runs; no-op if absent

from . import listing_flags, listings, model_batch, model_check, scorer_client
from .auth import DUMMY_PASSWORD_HASH, get_current_user, require_role, verify_password
from .db import Base, engine, get_db
from .logging_config import setup_logging
from .models import SystemLog, User, UserRole

setup_logging()

app = FastAPI(title="TRIDENT-Val AVM & Risk Triage Engine", version="1.0-poc")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(listing_flags.router)  # bad-parse flags (app DB only); before listings' /{record_id}
app.include_router(listings.router)  # read-only scraped-listings explorer (BigQuery / NDJSON)
app.include_router(model_check.router)  # model estimate for one listing (registry reads + scorer subprocess)
app.include_router(model_batch.router)  # model estimate for every new listing of the latest scrape run

# Signed-cookie session for the PoC login (see app.auth). The frontend never calls the
# backend cross-origin (Vite/nginx both proxy /api same-origin), so a plain cookie
# session needs no CORS credentials plumbing. SESSION_SECRET falls back to a known,
# non-secret PoC placeholder (matches the docker-compose Postgres password convention)
# so the app still boots locally; rotate it before any non-local use.
SESSION_SECRET = os.environ.get("SESSION_SECRET") or "trident-poc-insecure-default-change-me"
if not os.environ.get("SESSION_SECRET"):
    logger.warning("SESSION_SECRET is not set; using an insecure PoC-only default. "
                   "Set SESSION_SECRET before any non-local use.")
# Defaults to False so the cookie still works over plain http://localhost; flip to true
# once this sits behind real HTTPS so the browser refuses to send the cookie over http.
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=60 * 60 * 8,
                   https_only=SESSION_COOKIE_SECURE)


@app.on_event("startup")
def log_startup():
    Base.metadata.create_all(bind=engine)  # idempotent: only creates system_logs on existing DBs
    logger.info("TRIDENT-Val backend starting up (version {v})", v=app.version)


@app.on_event("shutdown")
def stop_scorers():
    scorer_client.shutdown_all()  # the long-lived model-check scorer processes must not outlive the backend


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as e:
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        logger.bind(context={
            "method": request.method, "path": request.url.path,
            "status_code": 500, "duration_ms": duration_ms, "error": str(e),
        }).error("{method} {path} -> 500 ({duration}ms) - unhandled exception: {error}",
                method=request.method, path=request.url.path, duration=duration_ms, error=str(e))
        raise
    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    level = "ERROR" if response.status_code >= 500 else \
            "WARNING" if response.status_code >= 400 else "INFO"
    logger.bind(context={
        "method": request.method, "path": request.url.path,
        "status_code": response.status_code, "duration_ms": duration_ms,
    }).log(level, "{method} {path} -> {status} ({duration}ms)",
          method=request.method, path=request.url.path,
          status=response.status_code, duration=duration_ms)
    return response

# ---------- schemas ----------

class LoginRequest(BaseModel):
    username: str
    password: str


# ---------- helpers ----------

def user_out(u: User) -> dict:
    return {"username": u.username, "role": u.role.value}


# ---------- endpoints ----------

@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/auth/login")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    # `password_ok` must be computed unconditionally, outside the `if` below — `or` would
    # short-circuit and skip verify_password entirely when user is None, making "user not
    # found" respond faster than "wrong password" and leaking which usernames exist via
    # response timing. Checking against a dummy hash keeps both paths equally slow.
    password_hash = user.password_hash if user else DUMMY_PASSWORD_HASH
    password_ok = verify_password(body.password, password_hash)
    if user is None or not password_ok:
        logger.bind(context={"attempted_username": body.username}).warning(
            "Failed login attempt for username '{username}'.", username=body.username)
        raise HTTPException(401, "Invalid username or password")
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["username"] = user.username
    logger.bind(actor=user.username, context={"role": user.role.value}).info(
        "User '{username}' logged in.", username=user.username)
    return user_out(user)


@app.post("/api/v1/auth/logout")
def logout(request: Request, user: User = Depends(get_current_user)):
    logger.bind(actor=user.username).info("User '{username}' logged out.", username=user.username)
    request.session.clear()
    return {"status": "ok"}


@app.get("/api/v1/auth/me")
def me(user: User = Depends(get_current_user)):
    return user_out(user)


# ---------- data profiles (ydata-profiling) ----------
# Static HTML reports written by scripts/generate_profile.py. Admin-gated because a profile
# shows per-column distributions and sample values of the underlying data. Served as-is and
# embedded by the frontend in an iframe (the report bundles its own Bootstrap/jQuery).
PROFILE_DIR = Path(os.environ.get("PROFILE_DIR") or Path(__file__).resolve().parents[2] / "reports" / "data_profile")
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


@app.get("/api/v1/reports/data-profile")
def list_data_profiles(user: User = Depends(require_role(UserRole.ADMIN))):
    files = sorted(PROFILE_DIR.glob("*.html"), key=lambda f: f.stat().st_mtime, reverse=True) \
        if PROFILE_DIR.is_dir() else []
    return {"items": [{
        "name": f.stem,
        "size_kb": round(f.stat().st_size / 1024),
        "generated_at": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(),
    } for f in files]}


@app.get("/api/v1/reports/data-profile/{name}")
def data_profile_report(name: str, user: User = Depends(require_role(UserRole.ADMIN))):
    if not _PROFILE_NAME.match(name):
        raise HTTPException(400, "Invalid report name")
    path = PROFILE_DIR / f"{name}.html"
    if not path.is_file():
        raise HTTPException(404, "No such data profile; generate it with scripts/generate_profile.py")
    return Response(content=path.read_text(encoding="utf-8"), media_type="text/html")


# ---------- logging / audit trail ----------

class ClientLogEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    level: str = Field("INFO", description="DEBUG | INFO | WARN | ERROR")
    logger_name: str = Field("frontend", alias="logger")
    message: str
    pid: Optional[int] = None
    context: Optional[dict] = None


def log_row(r: SystemLog) -> dict:
    return {
        "id": r.id, "timestamp": r.timestamp.isoformat() if r.timestamp else None,
        "source": r.source, "level": r.level, "logger": r.logger_name,
        "message": r.message, "pid": r.pid, "context": r.context, "actor": r.actor,
    }


@app.get("/api/v1/logs")
def list_logs(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.UNDERWRITER, UserRole.ADMIN)),
    source: Optional[str] = Query(None, pattern="^(backend|frontend)$"),
    level: Optional[str] = None,
    pid: Optional[int] = None,
    actor: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """Query the unified audit/operational log ledger. Restricted to Underwriter/Admin —
    this is the compliance-sensitive record of everyone's actions, not just current-state
    data. Flip to Depends(get_current_user) if Viewers should read it too."""
    q = db.query(SystemLog)
    if source:
        q = q.filter(SystemLog.source == source)
    if level:
        q = q.filter(SystemLog.level == level.upper())
    if actor:
        q = q.filter(SystemLog.actor == actor)
    if pid is not None:
        q = q.filter(SystemLog.pid == pid)
    if search:
        q = q.filter(SystemLog.message.ilike(f"%{search}%"))

    total = q.count()
    rows = (q.order_by(SystemLog.timestamp.desc())
             .offset((page - 1) * page_size).limit(page_size).all())
    return {"total": total, "page": page, "page_size": page_size,
            "items": [log_row(r) for r in rows]}


VALID_LOG_LEVELS = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
LEVEL_ALIASES = {"TRACE": "DEBUG", "WARN": "WARNING"}


CLIENT_LOG_BATCH_LIMIT = 50


@app.post("/api/v1/logs/client", status_code=204)
def log_client_event(entries: list[ClientLogEntry], request: Request):
    """Ingest a batch of frontend (loglevel) log entries into the shared ledger.

    Deliberately left open (no login required) — this is the endpoint through which
    the frontend reports its own errors, including pre-login and 401s themselves;
    gating it risks a 401-reporting-a-401 loop. Since it's unauthenticated, batch size
    is capped so it can't be used to flood system_logs with unbounded writes. `actor` is
    attached opportunistically from the session when one exists, never required, and
    never trusts a client-supplied value (ClientLogEntry has no actor field at all)."""
    if len(entries) > CLIENT_LOG_BATCH_LIMIT:
        raise HTTPException(400, f"Batch exceeds the {CLIENT_LOG_BATCH_LIMIT}-entry limit per request.")
    actor = request.session.get("username")
    for e in entries:
        raw = e.level.upper()
        level = LEVEL_ALIASES.get(raw, raw)
        if level not in VALID_LOG_LEVELS:
            level = "INFO"
        logger.bind(source="frontend", pid=e.pid, context=e.context,
                   logger_name=e.logger_name, actor=actor).log(
            level, "[frontend:{logger}] {message}", logger=e.logger_name, message=e.message)
