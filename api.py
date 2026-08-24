"""
Solo Studio API — FastAPI server with job management.

POST   /api/jobs          Create a new video job
GET    /api/jobs/{id}     Get job status + progress
GET    /api/jobs/{id}/download  Download final package (zip)
GET    /api/jobs          List recent jobs
"""
import ipaddress
import io
import json
import math
import os
import secrets
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import yaml
import auth_store
import job_store
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, BackgroundTasks, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from package_utils import LEGACY_FLAT_RUN_ID, atomic_write_text, compute_package_status, read_json_object, resolve_current_attempt_output_dir, update_json_file, write_package_manifest


# ── Config — auto-detect base dir (works in Docker and local) ──
APP_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = APP_DIR / "output"
MAX_DOWNLOAD_BYTES = int(os.getenv("SOLO_STUDIO_MAX_DOWNLOAD_BYTES", str(512 * 1024 * 1024)))
DEFAULT_JOBS_FILE = Path(os.getenv("SOLO_STUDIO_JOBS_FILE", str(APP_DIR / "jobs.json")))
JOBS_FILE = DEFAULT_JOBS_FILE
DATABASE_FILE = Path(os.getenv("SOLO_STUDIO_DATABASE_FILE", str(APP_DIR / "state" / "solo_studio.sqlite3")))
DATABASE_CONFIGURED = bool(os.getenv("SOLO_STUDIO_DATABASE_FILE", "").strip())
AUTH_DB_ENABLED = DATABASE_CONFIGURED
FRONTEND_DIR = APP_DIR / "frontend"
API_TOKEN = os.getenv("SOLO_STUDIO_API_TOKEN", "").strip()
TOKEN_IDENTITIES_FILE = Path(os.getenv("SOLO_STUDIO_TOKEN_IDENTITIES_FILE", "").strip()) if os.getenv("SOLO_STUDIO_TOKEN_IDENTITIES_FILE", "").strip() else None
REQUIRE_API_TOKEN = os.getenv("SOLO_STUDIO_REQUIRE_API_TOKEN", "").strip().lower() in {"1", "true", "yes"}
SESSION_COOKIE_NAME = "solo_studio_session"
SESSION_COOKIE_PATH = os.getenv("SOLO_STUDIO_SESSION_COOKIE_PATH", "/")
SESSION_MAX_AGE = int(os.getenv("SOLO_STUDIO_SESSION_MAX_AGE", "3600"))
COOKIE_SECURE = os.getenv("SOLO_STUDIO_COOKIE_SECURE", "1" if (REQUIRE_API_TOKEN or API_TOKEN) else "0").strip().lower() not in {"0", "false", "no"}
SESSION_TOKENS: dict[str, float] = {}
SESSION_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
SESSION_LOGIN_WINDOW = int(os.getenv("SOLO_STUDIO_SESSION_LOGIN_WINDOW", "300"))
SESSION_LOGIN_MAX_ATTEMPTS = int(os.getenv("SOLO_STUDIO_SESSION_LOGIN_MAX_ATTEMPTS", "10"))
SESSION_LOGIN_MAX_KEYS = int(os.getenv("SOLO_STUDIO_SESSION_LOGIN_MAX_KEYS", "10000"))
TRUST_PROXY_HEADERS = os.getenv("SOLO_STUDIO_TRUST_PROXY_HEADERS", "0").strip().lower() in {"1", "true", "yes"}
TRUSTED_PROXY_NETWORKS = tuple(
    ipaddress.ip_network(value.strip(), strict=False)
    for value in os.getenv("SOLO_STUDIO_TRUSTED_PROXY_NETWORKS", "127.0.0.1/32,::1/128").split(",")
    if value.strip()
)
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("SOLO_STUDIO_CORS_ORIGINS", "https://edgescout.tech").split(",")
    if origin.strip()
]

if REQUIRE_API_TOKEN and not API_TOKEN and TOKEN_IDENTITIES_FILE is None:
    raise RuntimeError("SOLO_STUDIO_API_TOKEN is required when SOLO_STUDIO_REQUIRE_API_TOKEN=1")
# Local development can run with token auth disabled. Docker/prod enables
# SOLO_STUDIO_REQUIRE_API_TOKEN=1 so accidental tokenless deployments fail closed.

app = FastAPI(title="Solo Studio API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _jsonable_no_nans(value):
    """Recursively replace NaN/Infinity payloads that JSON can't serialize."""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return value
    if isinstance(value, list):
        return [_jsonable_no_nans(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable_no_nans(item) for key, item in value.items()}
    return value


@app.exception_handler(RequestValidationError)
async def _handle_request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    del request
    details = _jsonable_no_nans(jsonable_encoder(exc.errors()))
    return JSONResponse(status_code=422, content={"detail": details})


@app.middleware("http")
async def support_video_api_prefix(request, call_next):
    """Allow direct container/local calls to /video/api/* as well as /api/*.

    Production Traefik strips /video before forwarding. Direct app smoke tests
    and local frontend sessions do not, so only API paths are normalized here;
    static /video/* remains the reverse proxy's responsibility.
    """
    path = request.scope.get("path", "")
    if path.startswith("/video/api/") or path == "/video/api":
        request.scope["path"] = path[len("/video"):]
    return await call_next(request)


# ── Models ──
class BriefRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=4000)
    target_audience: str = Field(default="general", max_length=200)
    duration_minutes: float = Field(default=1.0, ge=0.5, le=90, strict=True)
    platform: str = Field(default="youtube", max_length=64)
    tone: str = Field(default="professional", max_length=128)
    key_messages: list[str] = Field(default_factory=list, max_length=20)
    visual_style: str = Field(default="", max_length=2000)
    call_to_action: str = Field(default="", max_length=1000)


class OperatorSessionRequest(BaseModel):
    token: str = ""


class JobStatus(BaseModel):
    id: str
    topic: str
    target_audience: str = ""
    status: str  # queued, running, completed, failed
    progress: float = 0.0  # 0.0 to 1.0
    stage: str = "waiting"
    format: str = ""
    chapters: int = 0
    scenes: int = 0
    duration_seconds: float = 0
    platform: str = ""
    tone: str = ""
    key_messages: list[str] = Field(default_factory=list)
    visual_style: str = ""
    call_to_action: str = ""
    template: str = ""
    created_at: str
    completed_at: Optional[str] = None
    error: Optional[str] = None
    has_visuals: bool = False
    has_voiceover: bool = False
    has_clips: bool = False
    has_final_video: bool = False
    package_status: str = "not_started"
    artifact_summary: dict = Field(default_factory=dict)
    verified_clips: int = 0
    expected_scenes: int = 0


def _header_token(authorization: str | None, x_solo_studio_token: str | None) -> str:
    supplied = x_solo_studio_token or ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            supplied = value.strip()
    return supplied


def _token_owner(token: str) -> str | None:
    if API_TOKEN and secrets.compare_digest(token, API_TOKEN):
        return job_store.DEFAULT_OWNER_ID
    if TOKEN_IDENTITIES_FILE is None or not token:
        return None
    try:
        identities = json.loads(TOKEN_IDENTITIES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(identities, dict):
        return None
    for owner_id, configured in identities.items():
        if isinstance(owner_id, str) and isinstance(configured, str) and secrets.compare_digest(token, configured):
            return owner_id[:128] or None
    return None


def _prune_sessions() -> None:
    now = time.time()
    for session, expires_at in list(SESSION_TOKENS.items()):
        if expires_at <= now:
            SESSION_TOKENS.pop(session, None)
    if AUTH_DB_ENABLED:
        auth_store.prune_sessions(DATABASE_FILE)


def _login_client_keys(request: Request) -> tuple[str, ...]:
    peer = request.client.host if request.client else "unknown"
    try:
        address = ipaddress.ip_address(peer)
        trusted_peer = any(address in network for network in TRUSTED_PROXY_NETWORKS)
    except ValueError:
        trusted_peer = peer == "testclient"
    if TRUST_PROXY_HEADERS and trusted_peer:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        forwarded = forwarded or request.headers.get("x-real-ip", "").strip()
        try:
            forwarded = str(ipaddress.ip_address(forwarded)) if forwarded else ""
        except ValueError:
            forwarded = ""
        return (f"forwarded:{forwarded[:128]}",) if forwarded and forwarded != peer else (f"peer:{peer[:128]}",)
    return (f"peer:{peer[:128]}",)


def _duration_minutes_to_seconds(duration_minutes: float | int | str) -> int:
    if isinstance(duration_minutes, bool):
        raise ValueError("duration_minutes must be a real number")

    try:
        minutes = float(duration_minutes)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("duration_minutes must be a number")

    if not math.isfinite(minutes):
        raise ValueError("duration_minutes must be finite")

    if minutes < 0.5 or minutes > 90:
        raise ValueError("duration_minutes must be between 0.5 and 90")

    return int(minutes * 60)


def _allow_session_login(request: Request) -> bool:
    now = time.time()
    keys = _login_client_keys(request)
    for candidate, timestamps in list(SESSION_LOGIN_ATTEMPTS.items()):
        recent = [timestamp for timestamp in timestamps if timestamp > now - SESSION_LOGIN_WINDOW]
        if recent:
            SESSION_LOGIN_ATTEMPTS[candidate] = recent
        else:
            SESSION_LOGIN_ATTEMPTS.pop(candidate, None)
    if any(
        len(SESSION_LOGIN_ATTEMPTS.get(key, [])) >= SESSION_LOGIN_MAX_ATTEMPTS
        for key in keys
    ):
        return False
    max_stored_keys = max(SESSION_LOGIN_MAX_KEYS, 0)
    while (
        len(SESSION_LOGIN_ATTEMPTS) + len([key for key in keys if key not in SESSION_LOGIN_ATTEMPTS])
        > max_stored_keys
    ):
        if not SESSION_LOGIN_ATTEMPTS:
            break
        oldest_key = min(
            SESSION_LOGIN_ATTEMPTS,
            key=lambda candidate: SESSION_LOGIN_ATTEMPTS[candidate][-1],
        )
        SESSION_LOGIN_ATTEMPTS.pop(oldest_key, None)
    for key in keys:
        recent = [timestamp for timestamp in SESSION_LOGIN_ATTEMPTS.get(key, []) if timestamp > now - SESSION_LOGIN_WINDOW]
        recent.append(now)
        SESSION_LOGIN_ATTEMPTS[key] = recent
    return True


def _header_owner(authorization: str | None, x_solo_studio_token: str | None) -> str | None:
    return _token_owner(_header_token(authorization, x_solo_studio_token))


def _valid_header_token(authorization: str | None, x_solo_studio_token: str | None) -> bool:
    return _header_owner(authorization, x_solo_studio_token) is not None


def _set_authenticated_owner(request: Request, owner_id: str) -> None:
    request.state.owner_id = owner_id[:128] or job_store.DEFAULT_OWNER_ID


def _csrf_origin_allowed(request: Request, *, require_header: bool = False) -> bool:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    origin = request.headers.get("origin")
    if origin:
        return origin in ALLOWED_ORIGINS
    referer = request.headers.get("referer")
    if referer:
        parsed = urlparse(referer)
        return bool(parsed.scheme and parsed.netloc and f"{parsed.scheme}://{parsed.netloc}" in ALLOWED_ORIGINS)
    return not require_header


def require_api_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_solo_studio_token: str | None = Header(default=None),
    solo_studio_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> None:
    """Protect job routes with a header token or durable HttpOnly session."""
    if not API_TOKEN and TOKEN_IDENTITIES_FILE is None:
        _set_authenticated_owner(request, job_store.DEFAULT_OWNER_ID)
        return
    if not _csrf_origin_allowed(request, require_header=bool(solo_studio_session)):
        raise HTTPException(status_code=403, detail="Cross-origin state-changing request refused.")

    try:
        _prune_sessions()
    except job_store.JobStoreError as exc:
        raise HTTPException(status_code=503, detail="Authentication store unavailable") from exc
    if solo_studio_session:
        if AUTH_DB_ENABLED:
            try:
                session = auth_store.validate_session(DATABASE_FILE, solo_studio_session)
            except job_store.JobStoreError as exc:
                raise HTTPException(status_code=503, detail="Authentication store unavailable") from exc
            if session:
                _set_authenticated_owner(request, str(session["owner_id"]))
                return
        elif solo_studio_session in SESSION_TOKENS:
            _set_authenticated_owner(request, job_store.DEFAULT_OWNER_ID)
            return
    owner_id = _header_owner(authorization, x_solo_studio_token)
    if owner_id is None:
        if not _allow_session_login(request):
            raise HTTPException(
                status_code=429,
                detail="Too many failed authentication attempts. Try again later.",
                headers={"Retry-After": str(SESSION_LOGIN_WINDOW)},
            )
        raise HTTPException(
            status_code=401,
            detail="Solo Studio API token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    _set_authenticated_owner(request, owner_id)
    for key in _login_client_keys(request):
        SESSION_LOGIN_ATTEMPTS.pop(key, None)


@app.post("/api/auth/session")
async def create_session(
    login: OperatorSessionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_solo_studio_token: str | None = Header(default=None),
):
    """Exchange a manually entered operator token for an HttpOnly session cookie."""
    if (API_TOKEN or TOKEN_IDENTITIES_FILE is not None) and not _csrf_origin_allowed(request, require_header=True):
        raise HTTPException(status_code=403, detail="Cross-origin authentication request refused.")
    supplied = login.token.strip() or _header_token(authorization, x_solo_studio_token)
    owner_id = _token_owner(supplied) if (API_TOKEN or TOKEN_IDENTITIES_FILE is not None) else job_store.DEFAULT_OWNER_ID
    if (API_TOKEN or TOKEN_IDENTITIES_FILE is not None) and owner_id is None:
        if not _allow_session_login(request):
            raise HTTPException(
                status_code=429,
                detail="Too many failed session attempts. Try again later.",
                headers={"Retry-After": str(SESSION_LOGIN_WINDOW)},
            )
        raise HTTPException(
            status_code=401,
            detail="Solo Studio API token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    response = Response(status_code=204)
    if API_TOKEN or TOKEN_IDENTITIES_FILE is not None:
        for key in _login_client_keys(request):
            SESSION_LOGIN_ATTEMPTS.pop(key, None)
        try:
            _prune_sessions()
        except job_store.JobStoreError as exc:
            raise HTTPException(status_code=503, detail="Authentication store unavailable") from exc
        owner_id = owner_id or job_store.DEFAULT_OWNER_ID
        if AUTH_DB_ENABLED:
            try:
                session = auth_store.create_session(DATABASE_FILE, owner_id, SESSION_MAX_AGE)
            except job_store.JobStoreError as exc:
                raise HTTPException(status_code=503, detail="Authentication store unavailable") from exc
        else:
            session = secrets.token_urlsafe(32)
            SESSION_TOKENS[session] = time.time() + SESSION_MAX_AGE
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="strict",
            path=SESSION_COOKIE_PATH,
        )
    return response


@app.post("/api/auth/logout")
@app.delete("/api/auth/session")
async def delete_session(
    response: Response,
    request: Request,
    solo_studio_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    """Revoke the current durable or in-memory session and clear the browser cookie."""
    if (API_TOKEN or TOKEN_IDENTITIES_FILE is not None) and not _csrf_origin_allowed(request, require_header=bool(solo_studio_session)):
        raise HTTPException(status_code=403, detail="Cross-origin logout request refused.")
    if solo_studio_session:
        if AUTH_DB_ENABLED:
            try:
                auth_store.revoke_session(DATABASE_FILE, solo_studio_session)
            except job_store.JobStoreError as exc:
                raise HTTPException(status_code=503, detail="Authentication store unavailable") from exc
        SESSION_TOKENS.pop(solo_studio_session, None)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE_NAME, path=SESSION_COOKIE_PATH)
    return response


# ── Job store ──
def _uses_legacy_json_store() -> bool:
    """Keep test/one-time migration callers compatible without using JSON in prod."""
    return not DATABASE_CONFIGURED


def _load_jobs(*, limit: int = 50, owner_id: str | None = None) -> dict:
    if _uses_legacy_json_store():
        return read_json_object(JOBS_FILE)
    return {
        job["id"]: job
        for job in job_store.list_jobs(limit=limit, owner_id=owner_id, path=DATABASE_FILE)
    }


def _validate_job_records(jobs: dict) -> dict:
    if not isinstance(jobs, dict):
        raise ValueError("jobs store must contain an object")
    for key, job in jobs.items():
        if not isinstance(key, str) or not isinstance(job, dict):
            raise ValueError("legacy jobs store entries must be objects")
        key_id = job_store._validate_job_id(key)
        embedded_id = job.get("id")
        if not isinstance(embedded_id, str) or embedded_id != key or job_store._validate_job_id(embedded_id) != key_id:
            raise ValueError("legacy jobs store entries must contain a matching id")
    return jobs


def _sanitize_legacy_job_records(jobs: dict) -> dict:
    """Expose only the documented job contract from compatibility JSON."""
    public_keys = job_store.LEGACY_INPUT_KEYS | {
        "status", "progress", "stage", "created_at", "updated_at", "completed_at",
        "cancelled_at", "error", "max_retries", "next_attempt_at", "retry_count",
        "attempt", "stages",
    }
    return {
        job_id: {key: value for key, value in job.items() if key in public_keys}
        for job_id, job in jobs.items()
    }


PUBLIC_JOB_KEYS = frozenset({
    "id", "topic", "target_audience", "duration_seconds", "platform", "tone",
    "key_messages", "visual_style", "call_to_action", "template", "status",
    "progress", "stage", "format", "chapters", "scenes", "created_at",
    "completed_at", "error", "has_visuals", "has_voiceover", "has_clips",
    "has_final_video", "package_status", "artifact_summary", "verified_clips",
    "expected_scenes",
})


def _public_job_response(job: dict) -> dict:
    """Expose only the documented job contract on every backend."""
    if not isinstance(job, dict):
        raise ValueError("job response must be an object")
    return {key: value for key, value in job.items() if key in PUBLIC_JOB_KEYS}


def _load_jobs_for_api(*, limit: int = 50, owner_id: str | None = None) -> dict:
    try:
        jobs = _validate_job_records(_load_jobs(limit=limit, owner_id=owner_id))
        return _sanitize_legacy_job_records(jobs) if _uses_legacy_json_store() else jobs
    except (ValueError, OSError, job_store.JobStoreError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Jobs store is invalid and requires repair before job state can be read or changed.",
        ) from exc


def _owner_id_for_request(request: Request) -> str:
    return str(getattr(request.state, "owner_id", job_store.DEFAULT_OWNER_ID))[:128]


def _jobs_visible_to(request: Request, jobs: dict) -> dict:
    if not isinstance(jobs, dict) or any(not isinstance(job, dict) for job in jobs.values()):
        raise ValueError("legacy jobs store entries must be objects")
    owner_id = _owner_id_for_request(request)
    if owner_id == job_store.DEFAULT_OWNER_ID:
        return jobs
    return {job_id: job for job_id, job in jobs.items() if job.get("owner_id", job_store.DEFAULT_OWNER_ID) == owner_id}


def _get_visible_job(request: Request, job_id: str) -> dict | None:
    """Fetch one job without relying on the bounded recent-jobs listing."""
    owner_id = _owner_id_for_request(request)
    if _uses_legacy_json_store():
        return _jobs_visible_to(request, _load_jobs_for_api()).get(job_id)
    try:
        job = job_store.get_job(job_id, path=DATABASE_FILE)
    except (ValueError, OSError, job_store.JobStoreError) as exc:
        raise HTTPException(status_code=503, detail="Jobs store is invalid and requires repair before job state can be read or changed.") from exc
    if job is None or (owner_id != job_store.DEFAULT_OWNER_ID and job.get("owner_id", job_store.DEFAULT_OWNER_ID) != owner_id):
        return None
    return job


def _create_job_locked(job_id: str, job: dict, idempotency_key: str | None = None) -> tuple[dict, bool]:
    if not _uses_legacy_json_store():
        try:
            return job_store.create_job(
                job_id,
                job,
                owner_id=job.get("owner_id", job_store.DEFAULT_OWNER_ID),
                idempotency_key=idempotency_key or job.get("idempotency_key"),
                output_dir=OUTPUT_ROOT / job_id,
                initial_status=job.get("status", "queued"),
                path=DATABASE_FILE,
            )
        except job_store.JobStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="Jobs store is invalid and requires repair before new jobs can be created.",
            ) from exc

    normalized_key = idempotency_key.strip() if isinstance(idempotency_key, str) and idempotency_key.strip() else None
    owner_id = job.get("owner_id", job_store.DEFAULT_OWNER_ID)
    existing_job: dict[str, dict] = {}

    def add_job(all_jobs: dict) -> dict:
        if normalized_key is not None:
            for candidate in all_jobs.values():
                if (
                    isinstance(candidate, dict)
                    and candidate.get("owner_id", job_store.DEFAULT_OWNER_ID) == owner_id
                    and candidate.get("idempotency_key") == normalized_key
                ):
                    existing_job["job"] = candidate
                    return all_jobs
        job["idempotency_key"] = normalized_key
        all_jobs[job_id] = job
        return all_jobs

    try:
        stored = update_json_file(JOBS_FILE, add_job)
        if "job" in existing_job:
            return existing_job["job"], False
        return stored[job_id], True
    except (ValueError, OSError, job_store.JobStoreError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Jobs store is invalid and requires repair before new jobs can be created.",
        ) from exc


def _mark_job_claimable(job_id: str, owner_id: str) -> dict:
    """Make a fully materialized job visible to workers."""
    if not _uses_legacy_json_store():
        try:
            updated = job_store.update_fields(
                job_id,
                {"status": "queued"},
                expected_status="initializing",
                require_not_cancelled=True,
                path=DATABASE_FILE,
            )
        except (ValueError, OSError, job_store.JobStoreError) as exc:
            raise HTTPException(status_code=503, detail="Jobs store is invalid and requires repair before job state can change.") from exc
        if updated is None or updated.get("owner_id", job_store.DEFAULT_OWNER_ID) != owner_id:
            raise HTTPException(status_code=503, detail="Created job could not be made claimable.")
        if updated.get("status") != "queued":
            raise HTTPException(status_code=409, detail="Job was cancelled before it became claimable.")
        return updated

    def mark(payload: dict) -> dict:
        if not isinstance(payload, dict) or payload.get("id") != job_id:
            raise ValueError("created legacy job identity mismatch")
        if payload.get("owner_id", job_store.DEFAULT_OWNER_ID) != owner_id:
            raise ValueError("created legacy job owner mismatch")
        if payload.get("status") != "initializing" or payload.get("cancelled_at") is not None:
            raise ValueError("created legacy job is no longer claimable")
        payload["status"] = "queued"
        return payload

    try:
        updated = update_json_file(JOBS_FILE, lambda jobs: {**jobs, job_id: mark(dict(jobs[job_id]))})
        return updated[job_id]
    except (ValueError, OSError, job_store.JobStoreError) as exc:
        raise HTTPException(status_code=503, detail="Jobs store is invalid and requires repair before job state can change.") from exc


def _fail_initializing_job(job_id: str, owner_id: str, error: str) -> None:
    """Leave a durable creation failure terminal rather than stranded initializing."""
    safe_error = error[:500]
    if not _uses_legacy_json_store():
        try:
            job_store.update_fields(
                job_id,
                {
                    "status": "failed",
                    "error": safe_error,
                    "package_status": "failed",
                    "has_visuals": False,
                    "has_voiceover": False,
                    "has_clips": False,
                    "has_final_video": False,
                },
                expected_status="initializing",
                require_not_cancelled=True,
                path=DATABASE_FILE,
            )
        except Exception as exc:
            print(f"  [{job_id}] Could not terminally persist initialization failure: {exc}")
        return

    def fail(payload: dict) -> dict:
        if (
            isinstance(payload, dict)
            and payload.get("id") == job_id
            and payload.get("owner_id", job_store.DEFAULT_OWNER_ID) == owner_id
            and payload.get("status") == "initializing"
        ):
            payload.update(
                {
                    "status": "failed",
                    "error": safe_error,
                    "package_status": "failed",
                    "has_visuals": False,
                    "has_voiceover": False,
                    "has_clips": False,
                    "has_final_video": False,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        return payload

    try:
        update_json_file(JOBS_FILE, lambda jobs: {**jobs, job_id: fail(dict(jobs[job_id]))})
    except Exception as exc:
        print(f"  [{job_id}] Could not terminally persist initialization failure: {exc}")


def _add_job_locked(job_id: str, job: dict) -> dict:
    stored, _created = _create_job_locked(job_id, job, job.get("idempotency_key"))
    return stored


def _enrich_job(job: dict) -> dict:
    """Attach artifact-derived package fields without mutating stored state."""
    enriched = dict(job)
    job_id = job_store._validate_job_id(str(job.get("id", "")))
    try:
        output_dir = OUTPUT_ROOT / job_id if _uses_legacy_json_store() else resolve_current_attempt_output_dir(OUTPUT_ROOT, job)
    except (ValueError, OSError):
        output_dir = None
    enriched.pop("output_dir", None)
    if output_dir is not None and output_dir.exists():
        summary = compute_package_status(
            output_dir,
            job.get("status"),
            job.get("final_video_sha256"),
            job.get("final_video_plan_sha256"),
            job.get("id") if job.get("run_id") else None,
            job.get("attempt") if job.get("run_id") and isinstance(job.get("attempt"), int) else None,
            job.get("run_id"),
            job.get("source_digest") if job.get("run_id") else None,
        )
        enriched.update({
            "package_status": summary["package_status"],
            "has_visuals": summary["has_visuals"],
            "has_voiceover": summary["has_voiceover"],
            "has_clips": summary["has_clips"],
            "has_final_video": summary["has_final_video"],
            "artifact_summary": summary["artifacts"],
            "verified_clips": summary["verified_clips"],
            "expected_scenes": summary["expected_scenes"],
        })
    else:
        enriched["package_status"] = "not_started"
        enriched["has_visuals"] = False
        enriched["has_voiceover"] = False
        enriched["has_clips"] = False
        enriched["has_final_video"] = False
        enriched.pop("artifact_summary", None)
        enriched.pop("verified_clips", None)
        enriched.pop("expected_scenes", None)
    return enriched


def _enrich_jobs(jobs_to_enrich: list[dict]) -> list[dict]:
    """Enrich jobs in a worker thread so ffprobe never blocks the API event loop."""
    return [_enrich_job(job) for job in jobs_to_enrich]


def _write_download_manifest(job_dir: Path, job: dict) -> dict:
    """Refresh the package manifest in a worker thread before zipping."""
    return write_package_manifest(job_dir, _enrich_job(job))


def _open_directory_path_no_follow(path: Path) -> int:
    """Open every directory component without following symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open("/" if path.is_absolute() else ".", flags)
    try:
        for component in path.parts:
            if component in {"", "/", "."}:
                continue
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _iter_zip_files_no_follow(root: Path):
    """Yield ``(archive_name, fd, size)`` without path check/open races."""
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    root_fd = _open_directory_path_no_follow(root)

    def walk(directory_fd: int, prefix: str):
        try:
            with os.scandir(directory_fd) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    relative = f"{prefix}/{entry.name}" if prefix else entry.name
                    if (
                        entry.name.endswith(".lock")
                        or ".part-" in entry.name
                        or entry.name.endswith(".part")
                        or ".partial" in entry.name
                        or entry.name.endswith(".tmp")
                    ):
                        continue
                    if entry.is_symlink():
                        raise ValueError("symlinked artifact path")
                    if entry.is_dir(follow_symlinks=False):
                        child_fd = os.open(entry.name, directory_flags, dir_fd=directory_fd)
                        try:
                            yield from walk(child_fd, relative)
                        finally:
                            os.close(child_fd)
                    elif entry.is_file(follow_symlinks=False):
                        file_fd = os.open(entry.name, file_flags, dir_fd=directory_fd)
                        file_stat = os.fstat(file_fd)
                        if not stat.S_ISREG(file_stat.st_mode):
                            os.close(file_fd)
                            raise ValueError("non-regular artifact path")
                        # The caller owns the descriptor after yield and must close it.
                        yield relative, file_fd, file_stat.st_size
                    else:
                        raise ValueError("non-regular artifact path")
        finally:
            pass

    try:
        yield from walk(root_fd, "")
    finally:
        os.close(root_fd)


# ── Routes ──
@app.get("/api/health")
async def health():
    """Health/readiness probe for Traefik/container smoke checks."""
    return {
        "ok": True,
        "service": "solo-studio-video",
        "version": app.version,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/diagnostics", dependencies=[Depends(require_api_token)])
async def diagnostics():
    """Return non-secret operational state for operators and watchdogs."""
    try:
        disk = shutil.disk_usage(OUTPUT_ROOT)
        if DATABASE_CONFIGURED:
            queue = job_store.queue_snapshot(path=DATABASE_FILE)
            database = {"backend": "sqlite", "path": str(DATABASE_FILE), "ok": True}
        else:
            jobs = _validate_job_records(read_json_object(JOBS_FILE))
            counts: dict[str, int] = {}
            for job in jobs.values():
                status = str(job.get("status", "unknown"))
                counts[status] = counts.get(status, 0) + 1
            queue = {"jobs": counts, "queue_depth": counts.get("queued", 0), "stale_leases": None, "live_workers": []}
            database = {"backend": "json-legacy", "path": str(JOBS_FILE), "ok": True}
    except (OSError, ValueError, job_store.JobStoreError) as exc:
        raise HTTPException(status_code=503, detail="Diagnostics unavailable while operational state is invalid.") from exc
    return {
        "ok": True,
        "database": database,
        "queue": queue,
        "storage": {"free_bytes": disk.free, "total_bytes": disk.total},
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/")
async def index():
    """Serve the frontend."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/jobs", response_model=list[JobStatus], dependencies=[Depends(require_api_token)])
async def list_jobs(request: Request, limit: int = 10):
    """List recent jobs, newest first."""
    limit = max(1, min(limit, 500))
    if DATABASE_CONFIGURED:
        request_owner = _owner_id_for_request(request)
        visible_owner = None if request_owner == job_store.DEFAULT_OWNER_ID else request_owner
        jobs = list(_load_jobs_for_api(limit=limit, owner_id=visible_owner).values())
    else:
        all_jobs = _jobs_visible_to(request, _load_jobs_for_api())
        sorted_jobs = sorted(all_jobs.values(), key=lambda j: j.get('created_at', ''), reverse=True)
        jobs = sorted_jobs[:limit]
    return await run_in_threadpool(_enrich_jobs, jobs)


@app.post("/api/jobs", status_code=201, dependencies=[Depends(require_api_token)])
async def create_job(
    request: Request,
    brief: BriefRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Create a new video generation job."""
    job_id = uuid.uuid4().hex[:12]

    # Convert minutes to seconds
    try:
        duration_seconds = _duration_minutes_to_seconds(brief.duration_minutes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    job = {
        "id": job_id,
        "topic": brief.topic,
        "target_audience": brief.target_audience,
        "duration_seconds": duration_seconds,
        "platform": brief.platform,
        "tone": brief.tone,
        "key_messages": brief.key_messages,
        "visual_style": brief.visual_style,
        "call_to_action": brief.call_to_action,
        "status": "initializing",
        "progress": 0.0,
        "stage": "waiting",
        "format": "",
        "chapters": 0,
        "scenes": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "error": None,
        "has_visuals": False,
        "has_voiceover": False,
        "has_clips": False,
        "has_final_video": False,
        "package_status": "not_started",
        "owner_id": _owner_id_for_request(request),
    }

    stored_job, created = _create_job_locked(job_id, job, idempotency_key)
    if created:
        try:
            if AUTH_DB_ENABLED:
                auth_store.audit(DATABASE_FILE, _owner_id_for_request(request), "job.created", job_id=stored_job["id"])
            # Write the brief YAML for the worker only for a newly-created job.
            brief_path = OUTPUT_ROOT / stored_job["id"] / "brief.yaml"
            _write_brief_yaml(brief_path, stored_job)
            stored_job = _mark_job_claimable(stored_job["id"], stored_job.get("owner_id", job_store.DEFAULT_OWNER_ID))
        except HTTPException as exc:
            if exc.status_code != 409:
                _fail_initializing_job(stored_job["id"], stored_job.get("owner_id", job_store.DEFAULT_OWNER_ID), exc.detail)
            raise
        except Exception as exc:
            _fail_initializing_job(stored_job["id"], stored_job.get("owner_id", job_store.DEFAULT_OWNER_ID), str(exc))
            raise HTTPException(status_code=503, detail="Job initialization failed; the job was made terminal.") from exc

    enriched = await run_in_threadpool(_enrich_job, stored_job)
    return JSONResponse(content=_public_job_response(enriched), status_code=201 if created else 200)


@app.get("/api/templates")
async def list_templates():
    """List available brief templates."""
    templates_path = Path("/app/templates.json")
    if not templates_path.exists():
        templates_path = Path(__file__).parent / "templates.json"
    if not templates_path.exists():
        return []
    with open(templates_path) as f:
        return json.load(f)


@app.post("/api/jobs/from-template/{template_id}", status_code=201, dependencies=[Depends(require_api_token)])
async def create_job_from_template(
    request: Request,
    template_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Create a job from a pre-built template."""
    templates_path = Path(__file__).parent / "templates.json"
    if not templates_path.exists():
        raise HTTPException(status_code=404, detail="No templates available")

    with open(templates_path) as f:
        templates = json.load(f)

    template = next((t for t in templates if t['id'] == template_id), None)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")

    job_id = uuid.uuid4().hex[:12]
    try:
        duration_seconds = _duration_minutes_to_seconds(template.get("duration_minutes", 1.0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    job = {
        "id": job_id,
        "topic": template['topic'],
        "target_audience": template['target_audience'],
        "duration_seconds": duration_seconds,
        "platform": template['platform'],
        "tone": template['tone'],
        "key_messages": template.get('key_messages', []),
        "visual_style": template.get('visual_style', ''),
        "call_to_action": template.get('call_to_action', ''),
        "template": template_id,
        "status": "initializing",
        "progress": 0.0,
        "stage": "waiting",
        "format": "",
        "chapters": 0,
        "scenes": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "error": None,
        "has_visuals": False,
        "has_voiceover": False,
        "has_clips": False,
        "has_final_video": False,
        "package_status": "not_started",
        "owner_id": _owner_id_for_request(request),
    }

    stored_job, created = _create_job_locked(job_id, job, idempotency_key)
    if created:
        try:
            if AUTH_DB_ENABLED:
                auth_store.audit(DATABASE_FILE, _owner_id_for_request(request), "job.created_from_template", job_id=stored_job["id"])
            # Write brief YAML for the worker only for a newly-created job.
            brief_path = OUTPUT_ROOT / stored_job["id"] / "brief.yaml"
            _write_brief_yaml(brief_path, stored_job)
            stored_job = _mark_job_claimable(stored_job["id"], stored_job.get("owner_id", job_store.DEFAULT_OWNER_ID))
        except HTTPException as exc:
            if exc.status_code != 409:
                _fail_initializing_job(stored_job["id"], stored_job.get("owner_id", job_store.DEFAULT_OWNER_ID), exc.detail)
            raise
        except Exception as exc:
            _fail_initializing_job(stored_job["id"], stored_job.get("owner_id", job_store.DEFAULT_OWNER_ID), str(exc))
            raise HTTPException(status_code=503, detail="Job initialization failed; the job was made terminal.") from exc

    enriched = await run_in_threadpool(_enrich_job, stored_job)
    return JSONResponse(content=_public_job_response(enriched), status_code=201 if created else 200)


@app.get("/api/jobs/{job_id}", response_model=JobStatus, dependencies=[Depends(require_api_token)])
async def get_job_status(request: Request, job_id: str):
    """Get job status and progress."""
    job = _get_visible_job(request, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return await run_in_threadpool(_enrich_job, job)


@app.post("/api/jobs/{job_id}/cancel", response_model=JobStatus, dependencies=[Depends(require_api_token)])
async def cancel_job(request: Request, job_id: str):
    """Cancel a queued or running job without deleting its evidence."""
    existing_job = _get_visible_job(request, job_id)
    if existing_job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if DATABASE_CONFIGURED:
        try:
            job = job_store.cancel_job(job_id, path=DATABASE_FILE)
        except job_store.JobStoreError as exc:
            raise HTTPException(status_code=503, detail="Job store unavailable") from exc
        if AUTH_DB_ENABLED:
            auth_store.audit(DATABASE_FILE, _owner_id_for_request(request), "job.cancelled", job_id=job_id)
    else:
        def apply_cancel(jobs: dict) -> dict:
            current = jobs.get(job_id)
            if isinstance(current, dict) and current.get("status") not in {"completed", "failed", "cancelled"}:
                current.update({"status": "cancelled", "error": "cancelled by user", "completed_at": datetime.now(timezone.utc).isoformat()})
            return jobs
        job = update_json_file(JOBS_FILE, apply_cancel).get(job_id, existing_job)
    return await run_in_threadpool(_enrich_job, job)


@app.get("/api/jobs/{job_id}/download", dependencies=[Depends(require_api_token)])
async def download_job(request: Request, job_id: str):
    """Download the complete job output as a zip file."""
    job = _get_visible_job(request, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job['status'] != 'completed':
        raise HTTPException(status_code=400, detail="Job not yet completed")

    try:
        job_dir = OUTPUT_ROOT / job_id if _uses_legacy_json_store() else resolve_current_attempt_output_dir(OUTPUT_ROOT, job)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=500, detail="Unsafe artifact root") from exc
    if job_dir is None:
        raise HTTPException(status_code=404, detail="Job output not found")
    if not job_dir.exists() or job_dir.is_symlink():
        raise HTTPException(status_code=404, detail="Job output not found")
    root = OUTPUT_ROOT.resolve()
    resolved_job_dir = job_dir.resolve()
    if _uses_legacy_json_store() or job.get("run_id") == LEGACY_FLAT_RUN_ID:
        if resolved_job_dir.parent != root:
            raise HTTPException(status_code=500, detail="Unsafe artifact root")
    elif resolved_job_dir.parent.parent != root / job_id or resolved_job_dir.parent.name != "runs":
        raise HTTPException(status_code=500, detail="Unsafe artifact root")

    manifest = await run_in_threadpool(_write_download_manifest, job_dir, job)
    if not _uses_legacy_json_store() and manifest.get("package_status") == "not_started":
        raise HTTPException(status_code=404, detail="Current job output not found")

    # Create a bounded ZIP from regular files inside the job directory only.
    buf = io.BytesIO()
    total_bytes = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        try:
            for arcname, descriptor, size in _iter_zip_files_no_follow(job_dir):
                remaining = MAX_DOWNLOAD_BYTES - total_bytes
                if size > remaining:
                    os.close(descriptor)
                    descriptor = -1
                    raise HTTPException(status_code=413, detail="Job package exceeds the configured download limit")
                with os.fdopen(descriptor, "rb") as source:
                    data = source.read(remaining + 1)
                if len(data) > remaining:
                    raise HTTPException(status_code=413, detail="Job package exceeds the configured download limit")
                total_bytes += len(data)
                zf.writestr(arcname, data)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail="Unsafe artifact path") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="Artifact could not be safely opened") from exc
    buf.seek(0)

    safe_topic = "".join(c for c in job['topic'][:30] if c.isalnum() or c in ' _-').strip()
    filename = f"solo-studio-{safe_topic or job_id}.zip"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ── Helpers ──
def _write_brief_yaml(path: Path, job: dict):
    """Write a brief YAML file for the pipeline worker."""
    payload = {
        "topic": job["topic"],
        "target_audience": job["target_audience"],
        "duration_seconds": job["duration_seconds"],
        "platform": job["platform"],
        "tone": job["tone"],
        "key_messages": job.get("key_messages", []),
        "visual_style": job.get("visual_style", ""),
        "call_to_action": job.get("call_to_action", ""),
    }
    atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


# Mount frontend static files — serve everything under /
# API routes (/api/*) take priority over static files
FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
