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
import time
import uuid
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import yaml
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, BackgroundTasks, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from package_utils import compute_package_status, read_json_object, update_json_file, write_package_manifest


# ── Config — auto-detect base dir (works in Docker and local) ──
APP_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = APP_DIR / "output"
JOBS_FILE = Path(os.getenv("SOLO_STUDIO_JOBS_FILE", str(APP_DIR / "jobs.json")))
FRONTEND_DIR = APP_DIR / "frontend"
API_TOKEN = os.getenv("SOLO_STUDIO_API_TOKEN", "").strip()
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

if REQUIRE_API_TOKEN and not API_TOKEN:
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
    topic: str
    target_audience: str = "general"
    duration_minutes: float = Field(default=1.0, ge=0.5, le=90, strict=True)
    platform: str = "youtube"
    tone: str = "professional"
    key_messages: list[str] = []
    visual_style: str = ""
    call_to_action: str = ""


class OperatorSessionRequest(BaseModel):
    token: str = ""


class JobStatus(BaseModel):
    id: str
    topic: str
    status: str  # queued, running, completed, failed
    progress: float  # 0.0 to 1.0
    stage: str
    format: str = ""
    chapters: int = 0
    scenes: int = 0
    duration_seconds: float = 0
    created_at: str
    completed_at: Optional[str] = None
    error: Optional[str] = None
    has_visuals: bool = False
    has_voiceover: bool = False
    has_clips: bool = False
    has_final_video: bool = False
    package_status: str = "not_started"


def _header_token(authorization: str | None, x_solo_studio_token: str | None) -> str:
    supplied = x_solo_studio_token or ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            supplied = value.strip()
    return supplied


def _prune_sessions() -> None:
    now = time.time()
    for session, expires_at in list(SESSION_TOKENS.items()):
        if expires_at <= now:
            SESSION_TOKENS.pop(session, None)


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


def _valid_header_token(authorization: str | None, x_solo_studio_token: str | None) -> bool:
    supplied = _header_token(authorization, x_solo_studio_token)
    return bool(supplied and API_TOKEN and secrets.compare_digest(supplied, API_TOKEN))


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
    """Protect job routes with a header token or short-lived HttpOnly session."""
    if not API_TOKEN:
        return
    if not _csrf_origin_allowed(request, require_header=bool(solo_studio_session)):
        raise HTTPException(status_code=403, detail="Cross-origin state-changing request refused.")

    _prune_sessions()
    if solo_studio_session and solo_studio_session in SESSION_TOKENS:
        return
    if not _valid_header_token(authorization, x_solo_studio_token):
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
    if API_TOKEN and not _csrf_origin_allowed(request, require_header=True):
        raise HTTPException(status_code=403, detail="Cross-origin authentication request refused.")
    supplied = login.token.strip() or _header_token(authorization, x_solo_studio_token)
    if API_TOKEN and not (supplied and secrets.compare_digest(supplied, API_TOKEN)):
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
    if API_TOKEN:
        for key in _login_client_keys(request):
            SESSION_LOGIN_ATTEMPTS.pop(key, None)
        _prune_sessions()
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
    """Revoke the current in-memory session and clear the browser cookie."""
    if API_TOKEN and not _csrf_origin_allowed(request, require_header=bool(solo_studio_session)):
        raise HTTPException(status_code=403, detail="Cross-origin logout request refused.")
    if solo_studio_session:
        SESSION_TOKENS.pop(solo_studio_session, None)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE_NAME, path=SESSION_COOKIE_PATH)
    return response


# ── Job store (JSON file persistence; API/worker writes use locked updates) ──
def _load_jobs() -> dict:
    return read_json_object(JOBS_FILE)


def _load_jobs_for_api() -> dict:
    try:
        return _load_jobs()
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Jobs store is invalid and requires repair before job state can be read or changed.",
        ) from exc


def _add_job_locked(job_id: str, job: dict) -> dict:
    def add_job(all_jobs: dict) -> dict:
        all_jobs[job_id] = job
        return all_jobs

    try:
        return update_json_file(JOBS_FILE, add_job)
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Jobs store is invalid and requires repair before new jobs can be created.",
        ) from exc


def _enrich_job(job: dict) -> dict:
    """Attach artifact-derived package fields without mutating stored state."""
    enriched = dict(job)
    output_dir = OUTPUT_ROOT / job["id"]
    if output_dir.exists():
        summary = compute_package_status(output_dir, job.get("status"))
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
        enriched.setdefault("package_status", "not_started")
        enriched.setdefault("has_clips", False)
        enriched.setdefault("has_final_video", False)
    return enriched


def _enrich_jobs(jobs_to_enrich: list[dict]) -> list[dict]:
    """Enrich jobs in a worker thread so ffprobe never blocks the API event loop."""
    return [_enrich_job(job) for job in jobs_to_enrich]


def _write_download_manifest(job_dir: Path, job: dict) -> None:
    """Refresh the package manifest in a worker thread before zipping."""
    write_package_manifest(job_dir, _enrich_job(job))


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


@app.get("/")
async def index():
    """Serve the frontend."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/jobs", dependencies=[Depends(require_api_token)])
async def list_jobs(limit: int = 10):
    """List recent jobs, newest first."""
    all_jobs = _load_jobs_for_api()
    sorted_jobs = sorted(all_jobs.values(), key=lambda j: j.get('created_at', ''), reverse=True)
    return await run_in_threadpool(_enrich_jobs, sorted_jobs[:limit])


@app.post("/api/jobs", status_code=201, dependencies=[Depends(require_api_token)])
async def create_job(brief: BriefRequest):
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
        "status": "queued",
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
    }

    _add_job_locked(job_id, job)

    # Write the brief YAML for the worker
    brief_path = OUTPUT_ROOT / job_id / "brief.yaml"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    _write_brief_yaml(brief_path, job)

    enriched = await run_in_threadpool(_enrich_job, job)
    return JSONResponse(content=enriched, status_code=201)


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
async def create_job_from_template(template_id: str):
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
        "status": "queued",
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
    }

    _add_job_locked(job_id, job)

    # Write brief YAML for the worker
    brief_path = OUTPUT_ROOT / job_id / "brief.yaml"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    _write_brief_yaml(brief_path, job)

    enriched = await run_in_threadpool(_enrich_job, job)
    return JSONResponse(content=enriched, status_code=201)


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_api_token)])
async def get_job_status(job_id: str):
    """Get job status and progress."""
    all_jobs = _load_jobs_for_api()
    if job_id not in all_jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return await run_in_threadpool(_enrich_job, all_jobs[job_id])


@app.get("/api/jobs/{job_id}/download", dependencies=[Depends(require_api_token)])
async def download_job(job_id: str):
    """Download the complete job output as a zip file."""
    all_jobs = _load_jobs_for_api()
    if job_id not in all_jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = all_jobs[job_id]
    if job['status'] != 'completed':
        raise HTTPException(status_code=400, detail="Job not yet completed")

    job_dir = OUTPUT_ROOT / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job output not found")

    await run_in_threadpool(_write_download_manifest, job_dir, job)

    # Create zip in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(job_dir.rglob("*")):
            if f.is_file():
                arcname = str(f.relative_to(job_dir))
                zf.write(f, arcname)
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
    with open(path, 'w') as f:
        yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)


# Mount frontend static files — serve everything under /
# API routes (/api/*) take priority over static files
FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
