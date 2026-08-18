"""
Solo Studio API — FastAPI server with job management.

POST   /api/jobs          Create a new video job
GET    /api/jobs/{id}     Get job status + progress
GET    /api/jobs/{id}/download  Download final package (zip)
GET    /api/jobs          List recent jobs
"""
import json, uuid, shutil, zipfile, io, time, os, secrets
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from package_utils import compute_package_status, read_json_object, update_json_file, write_package_manifest


# ── Config — auto-detect base dir (works in Docker and local) ──
APP_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = APP_DIR / "output"
JOBS_FILE = APP_DIR / "jobs.json"
FRONTEND_DIR = APP_DIR / "frontend"
API_TOKEN = os.getenv("SOLO_STUDIO_API_TOKEN", "").strip()
REQUIRE_API_TOKEN = os.getenv("SOLO_STUDIO_REQUIRE_API_TOKEN", "").strip().lower() in {"1", "true", "yes"}
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
    duration_minutes: float = Field(default=1.0, ge=0.5, le=90)
    platform: str = "youtube"
    tone: str = "professional"
    key_messages: list[str] = []
    visual_style: str = ""
    call_to_action: str = ""


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


def require_api_token(
    authorization: str | None = Header(default=None),
    x_solo_studio_token: str | None = Header(default=None),
) -> None:
    """Protect job state/mutation routes when SOLO_STUDIO_API_TOKEN is set."""
    if not API_TOKEN:
        return

    supplied = x_solo_studio_token or ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            supplied = value.strip()

    if not supplied or not secrets.compare_digest(supplied, API_TOKEN):
        raise HTTPException(
            status_code=401,
            detail="Solo Studio API token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


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
    duration_seconds = int(brief.duration_minutes * 60)

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
    duration_seconds = int(template['duration_minutes'] * 60)

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
    uvicorn.run(app, host="0.0.0.0", port=8000)
