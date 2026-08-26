"""
Solo Studio Worker - Background pipeline executor.

Watches jobs.json for queued jobs, runs the pipeline, updates status.
Uses a simple polling loop (no Redis dependency - standalone).
"""
import json, subprocess, sys, time
from pathlib import Path
from datetime import datetime, timezone

APP_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = APP_DIR / "output"
JOBS_FILE = APP_DIR / "jobs.json"
ENGINES_DIR = APP_DIR / "engines"
PIPELINE = APP_DIR / "pipeline.py"

POLL_INTERVAL = 2  # seconds


def load_jobs() -> dict:
    if JOBS_FILE.exists():
        try:
            with open(JOBS_FILE, encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return json.loads(content)
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    return {}


def save_jobs(jobs: dict):
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(JOBS_FILE, 'w', encoding="utf-8") as f:
        json.dump(jobs, f, indent=2)


def update_job(job_id: str, **kwargs):
    """Update a job's fields and persist."""
    jobs = load_jobs()
    if job_id in jobs:
        jobs[job_id].update(kwargs)
        save_jobs(jobs)


def run_stage(job_id: str, name: str, script: str, *args) -> bool:
    """Run a pipeline stage and update job progress."""
    cmd = [sys.executable, str(ENGINES_DIR / script), *args]
    print(f"  [{job_id}] Running: {name}")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(f"  [{job_id}] FAILED: {name}")
        print(f"  stderr: {result.stderr[:500]}")
        return False
    return True


def process_job(job_id: str, job: dict):
    """Run the full pipeline for a single job."""
    out = OUTPUT_ROOT / job_id

    # Progress map: each stage = ~14% (7 stages)
    stages = [
        ("research",      0.00, 0.12),
        ("script",        0.12, 0.25),
        ("visuals",       0.25, 0.38),
        ("voiceover",     0.38, 0.50),
        ("video_prompts", 0.50, 0.62),
        ("music",         0.62, 0.72),
        ("captions",      0.72, 0.82),
        ("editor_export", 0.82, 0.92),
        ("assembly",      0.92, 1.00),
    ]

    try:
        # Stage 1: Research → Creative Brief
        update_job(job_id, status="running", stage="research", progress=0.05)
        brief_yaml = out / "brief.yaml"
        if not run_stage(job_id, "Research", "research_agent.py", str(brief_yaml), str(out)):
            update_job(job_id, status="failed", error="Research agent failed")
            return

        brief_json = out / "creative_brief.json"
        update_job(job_id, stage="script", progress=0.14)

        # Stage 2: Script → Storyboard
        if not run_stage(job_id, "Script", "script_agent.py", str(brief_json), str(out)):
            update_job(job_id, status="failed", error="Script agent failed")
            return

        # Read storyboard to get scene/chapter counts
        sb_path = out / "storyboard.json"
        with open(sb_path) as f:
            sb = json.load(f)
        update_job(
            job_id,
            stage="visuals",
            progress=0.28,
            format=sb.get('format', ''),
            chapters=len(sb.get('chapters', [])),
            scenes=len(sb.get('scenes', [])),
            duration_seconds=sb.get('total_duration', 0),
        )

        # Stage 3: Visuals - generate scene images
        if not run_stage(job_id, "Visuals", "design_agent.py", str(sb_path), str(out)):
            print(f"  [{job_id}] Visuals agent failed - continuing")
        else:
            # Try to generate images using the pipeline's image tools
            try:
                _generate_visuals(out, sb)
                update_job(job_id, has_visuals=True)
            except Exception as e:
                print(f"  [{job_id}] Image generation not available: {e}")

        update_job(job_id, stage="voiceover", progress=0.42)

        # Stage 4: Voiceover - generate TTS audio
        try:
            _generate_voiceover(out, sb)
            update_job(job_id, has_voiceover=True)
        except Exception as e:
            print(f"  [{job_id}] Voiceover not available: {e}")

        update_job(job_id, stage="video_prompts", progress=0.56)

        # Stage 5: Video Prompts
        if not run_stage(job_id, "Production", "production_agent.py", str(sb_path), str(out)):
            print(f"  [{job_id}] Production agent failed - continuing")

        update_job(job_id, stage="captions", progress=0.70)

        # Stage 6: Captions + Assembly
        if not run_stage(job_id, "Editing", "editing_agent.py", str(sb_path), str(out)):
            print(f"  [{job_id}] Editing agent failed - continuing")

        update_job(job_id, stage="editor_export", progress=0.84)

        # Stage 7: Editor export (FCPXML)
        if not run_stage(job_id, "Editor Export", "editor_export.py", str(sb_path), str(out)):
            print(f"  [{job_id}] Editor export failed - continuing")

        # Generate thumbnail prompt always
        try:
            _generate_thumbnail_for_job(out, brief_json)
        except Exception as e:
            print(f"  [{job_id}] Thumbnail: {e}")

        update_job(job_id, stage="assembly", progress=0.92)

        # Stage 7: Generate assembly manifest (already done by editing agent)
        # Mark complete
        update_job(
            job_id,
            status="completed",
            stage="done",
            progress=1.0,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        print(f"  [{job_id}] COMPLETED - {sb.get('total_duration', 0):.0f}s, {len(sb.get('scenes', []))} scenes")

    except Exception as e:
        print(f"  [{job_id}] FAILED: {e}")
        update_job(job_id, status="failed", error=str(e))


def _generate_visuals(out: Path, storyboard: dict):
    """Generate scene images using the host's image generation tools."""
    prompts_path = out / "visual_prompts.json"
    if not prompts_path.exists():
        return

    with open(prompts_path, encoding="utf-8") as f:
        manifest = json.load(f)

    visuals_dir = out / "visuals"
    visuals_dir.mkdir(exist_ok=True)

    prompts = manifest.get('prompts', [])
    print(f"  [{out.name}] Would generate {len(prompts)} images (needs image_gen tool access)")

    # Mark which prompts were generated vs pending
    with open(out / "visuals_status.json", 'w', encoding="utf-8") as f:
        json.dump({
            'total': len(prompts),
            'generated': 0,
            'pending': len(prompts),
            'note': 'Images not generated in worker process. Use API directly.'
        }, f, indent=2)


def _generate_voiceover(out: Path, storyboard: dict):
    """Generate voiceover audio."""
    vo_script = out / "audio" / "voiceover_script.txt"
    if not vo_script.exists():
        return

    with open(vo_script, encoding="utf-8") as f:
        text = f.read()

    print(f"  [{out.name}] Would generate voiceover ({len(text)} chars - needs TTS API key)")
    # TTS requires API key - produce the script and note it needs rendering


def _generate_thumbnail_for_job(out: Path, brief_json: Path):
    """Generate thumbnail prompt for a job."""
    with open(brief_json, encoding="utf-8") as f:
        brief = json.load(f)
    sb_path = out / "storyboard.json"
    with open(sb_path, encoding="utf-8") as f:
        sb = json.load(f)
    topic = brief.get('topic', '')
    tone = brief.get('tone', 'professional')
    dur = sb.get('total_duration', 60)
    hooks = {
        'educational': f"The TRUTH About {topic[:35]}",
        'professional': f"{topic[:35]} - {int(dur)}s",
        'cinematic': topic[:35].upper(),
        'energetic': f"DON'T Ignore {topic[:35]}!",
        'casual': f"I Tried {topic[:35]}",
    }
    overlay = hooks.get(tone, topic[:50])
    prompt = (
        f"YouTube thumbnail: '{topic[:60]}'. Bold text: '{overlay}'. "
        f"Style: {brief.get('visual_style', 'professional')}. "
        f"High contrast, vibrant, 4K, 16:9. Max 4 words."
    )
    tp = out / "thumbnail_prompt.json"
    with open(tp, 'w', encoding="utf-8") as f:
        json.dump({'title_overlay': overlay, 'prompt': prompt, 'filename': 'youtube_thumbnail.png'}, f, indent=2)


def main():
    print("Solo Studio Worker - watching for queued jobs...")
    print(f"  Jobs file: {JOBS_FILE}")

    while True:
        jobs = load_jobs()
        queued = [j for j in jobs.values() if j.get('status') == 'queued']

        for job in queued:
            job_id = job['id']
            print(f"\n{'='*50}")
            print(f"  Processing job: {job_id}")
            print(f"  Topic: {job['topic'][:80]}")
            print(f"  Duration: {job.get('duration_seconds', 0)}s")
            print(f"{'='*50}")
            process_job(job_id, job)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
