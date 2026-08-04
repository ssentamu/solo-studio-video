"""
Solo Studio API — FastAPI server with job management.

POST   /api/jobs          Create a new video job
GET    /api/jobs/{id}     Get job status + progress
GET    /api/jobs/{id}/download  Download final package (zip)
GET    /api/jobs          List recent jobs
"""
import json, uuid, shutil, zipfile, io, time
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ── Config ──
OUTPUT_ROOT = Path("/opt/data/solo-studio-video/output")
JOBS_FILE = Path("/opt/data/solo-studio-video/jobs.json")
FRONTEND_DIR = Path("/opt/data/solo-studio-video/frontend")

app = FastAPI(title="Solo Studio API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# ── Job store (in-memory + JSON file for persistence) ──
def _load_jobs() -> dict:
    if JOBS_FILE.exists():
        with open(JOBS_FILE) as f:
            return json.load(f)
    return {}


def _save_jobs(jobs: dict):
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(JOBS_FILE, 'w') as f:
        json.dump(jobs, f, indent=2)


jobs = _load_jobs()


def get_job(job_id: str) -> dict:
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


# ── Routes ──
@app.get("/")
async def index():
    """Serve the frontend."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/jobs")
async def list_jobs(limit: int = 10):
    """List recent jobs, newest first."""
    all_jobs = _load_jobs()
    sorted_jobs = sorted(all_jobs.values(), key=lambda j: j.get('created_at', ''), reverse=True)
    return sorted_jobs[:limit]


@app.post("/api/jobs", status_code=201)
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
    }

    jobs[job_id] = job
    _save_jobs(jobs)

    # Write the brief YAML for the worker
    brief_path = OUTPUT_ROOT / job_id / "brief.yaml"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    _write_brief_yaml(brief_path, job)

    return JSONResponse(content=job, status_code=201)


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


@app.post("/api/jobs/from-template/{template_id}", status_code=201)
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
    }

    all_jobs = _load_jobs()
    all_jobs[job_id] = job
    _save_jobs(all_jobs)

    # Write brief YAML for the worker
    brief_path = OUTPUT_ROOT / job_id / "brief.yaml"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    _write_brief_yaml(brief_path, job)

    return JSONResponse(content=job, status_code=201)


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Get job status and progress."""
    all_jobs = _load_jobs()
    if job_id not in all_jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return all_jobs[job_id]


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str):
    """Download the complete job output as a zip file."""
    all_jobs = _load_jobs()
    if job_id not in all_jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = all_jobs[job_id]
    if job['status'] != 'completed':
        raise HTTPException(status_code=400, detail="Job not yet completed")

    job_dir = OUTPUT_ROOT / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job output not found")

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
    lines = [
        f"topic: \"{job['topic']}\"",
        f"target_audience: \"{job['target_audience']}\"",
        f"duration_seconds: {job['duration_seconds']}",
        f"platform: \"{job['platform']}\"",
        f"tone: \"{job['tone']}\"",
        f"key_messages:",
    ]
    for msg in job.get('key_messages', []):
        lines.append(f'  - "{msg}"')
    lines.append(f'visual_style: "{job.get("visual_style", "")}"')
    lines.append(f'call_to_action: "{job.get("call_to_action", "")}"')

    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


# Mount frontend static files — serve everything under /
# API routes (/api/*) take priority over static files
FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
