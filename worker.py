"""
Solo Studio Worker — Background pipeline executor.

Watches jobs.json for queued jobs, runs the pipeline, updates status.
Uses a simple polling loop (no Redis dependency — standalone).
"""
import json, os, subprocess, sys, time, shutil
from pathlib import Path
from datetime import datetime, timezone

from package_utils import clear_generated_artifacts, read_json_object, update_json_file, write_package_manifest

APP_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = APP_DIR / "output"
JOBS_FILE = Path(os.getenv("SOLO_STUDIO_JOBS_FILE", str(APP_DIR / "jobs.json")))
ENGINES_DIR = APP_DIR / "engines"
PIPELINE = APP_DIR / "pipeline.py"

POLL_INTERVAL = 2  # seconds


def load_jobs() -> dict:
    return read_json_object(JOBS_FILE)


def update_job(job_id: str, **kwargs):
    """Update a job's fields and persist."""
    def apply_update(jobs: dict) -> dict:
        if job_id in jobs:
            jobs[job_id].update(kwargs)
        return jobs

    update_json_file(JOBS_FILE, apply_update)


def run_stage(job_id: str, name: str, script: str, *args, timeout: int | None = None) -> bool:
    """Run a pipeline stage and update job progress."""
    cmd = [sys.executable, str(ENGINES_DIR / script), *args]
    print(f"  [{job_id}] Running: {name}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout or int(os.getenv("SOLO_STUDIO_STAGE_TIMEOUT", "300")),
        )
    except subprocess.TimeoutExpired:
        print(f"  [{job_id}] FAILED: {name} timed out")
        return False
    except OSError as exc:
        print(f"  [{job_id}] FAILED: {name} could not start: {exc}")
        return False
    if result.returncode != 0:
        print(f"  [{job_id}] FAILED: {name}")
        print(f"  stderr: {result.stderr[:500]}")
        return False
    return True


def process_job(job_id: str, job: dict):
    """Run the full pipeline for a single job."""
    out = OUTPUT_ROOT / job_id

    try:
        clear_generated_artifacts(out)

        # Stage 1: Research → Creative Brief
        update_job(job_id, status="running", stage="research", progress=0.05)
        brief_yaml = out / "brief.yaml"
        if not run_stage(job_id, "Research", "research_agent.py", str(brief_yaml), str(out)):
            _fail_job(job_id, out, job, "Research agent failed")
            return

        brief_json = out / "creative_brief.json"
        update_job(job_id, stage="script", progress=0.14)

        # Stage 2: Script → Storyboard
        if not run_stage(job_id, "Script", "script_agent.py", str(brief_json), str(out)):
            _fail_job(job_id, out, job, "Script agent failed")
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

        # Stage 3: Visuals — generate scene images
        if not run_stage(job_id, "Visuals", "design_agent.py", str(sb_path), str(out)):
            _fail_job(job_id, out, job, "Visuals agent failed")
            return
        else:
            # Try to generate images using the pipeline's image tools
            try:
                update_job(job_id, has_visuals=_generate_visuals(out, sb))
            except Exception as e:
                print(f"  [{job_id}] Image generation not available: {e}")

        update_job(job_id, stage="voiceover", progress=0.42)

        # Stage 4: Voiceover — generate TTS audio
        try:
            update_job(job_id, has_voiceover=_generate_voiceover(out, sb))
        except Exception as e:
            print(f"  [{job_id}] Voiceover not available: {e}")

        update_job(job_id, stage="video_prompts", progress=0.56)

        # Stage 5: Video Prompts
        if not run_stage(job_id, "Production", "production_agent.py", str(sb_path), str(out)):
            _fail_job(job_id, out, job, "Production agent failed")
            return

        update_job(job_id, stage="video_generation", progress=0.64)

        # Stage 5b: Video generation plan / safe dry-run.
        vp_path = out / "video_prompts.json"
        if vp_path.exists():
            try:
                scene_count = len(json.loads(vp_path.read_text()).get("scenes", []))
            except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
                scene_count = 1
            provider_timeout_seconds = int(os.getenv("SOLO_STUDIO_HIGGSFIELD_TIMEOUT", "900"))
            # Per scene: provider wait + bounded download + ffprobe verification,
            # plus a stage-level margin for process startup and serialization.
            provider_timeout = (provider_timeout_seconds + 120 + 30) * max(scene_count, 1) + 60
            if not run_stage(
                job_id,
                "Video Generation Plan",
                "generation_agent.py",
                str(vp_path),
                str(out),
                timeout=provider_timeout,
            ):
                _fail_job(job_id, out, job, "Generation plan failed")
                return
        else:
            _fail_job(job_id, out, job, "Video prompts missing before generation stage")
            return

        update_job(job_id, stage="music", progress=0.72)
        # Music is currently emitted as a prompt by the production agent; real
        # audio generation is a later provider-integration stage.

        update_job(job_id, stage="captions", progress=0.76)

        # Stage 6: Captions + Assembly
        if not run_stage(job_id, "Editing", "editing_agent.py", str(sb_path), str(out)):
            _fail_job(job_id, out, job, "Editing agent failed")
            return

        update_job(job_id, stage="editor_export", progress=0.84)

        # Stage 7: Editor export (FCPXML)
        if not run_stage(job_id, "Editor Export", "editor_export.py", str(sb_path), str(out)):
            _fail_job(job_id, out, job, "Editor export failed")
            return

        # Generate thumbnail prompt always
        try:
            _generate_thumbnail_for_job(out, brief_json)
        except Exception as e:
            print(f"  [{job_id}] Thumbnail: {e}")

        update_job(job_id, stage="assembly", progress=0.92)

        completed_at = datetime.now(timezone.utc).isoformat()
        manifest = write_package_manifest(
            out,
            _manifest_job_snapshot(
                job_id,
                job,
                status="completed",
                stage="done",
                progress=1.0,
                completed_at=completed_at,
            ),
        )
        update_job(
            job_id,
            status="completed",
            stage="done",
            progress=1.0,
            completed_at=completed_at,
            package_status=manifest["package_status"],
            has_visuals=manifest["has_visuals"],
            has_voiceover=manifest["has_voiceover"],
            has_clips=manifest["has_clips"],
            has_final_video=manifest["has_final_video"],
            verified_clips=manifest["verified_clips"],
        )
        print(f"  [{job_id}] COMPLETED — {sb.get('total_duration', 0):.0f}s, {len(sb.get('scenes', []))} scenes")

    except Exception as e:
        print(f"  [{job_id}] FAILED: {e}")
        try:
            _fail_job(job_id, out, job, str(e))
        except Exception as finalizer_exc:
            print(f"  [{job_id}] FAILED to finalize job failure: {finalizer_exc}")


def _fail_job(job_id: str, out: Path, job: dict, error: str):
    """Persist a failed terminal state plus an artifact-derived manifest."""
    try:
        manifest = write_package_manifest(
            out,
            _manifest_job_snapshot(job_id, job, status="failed", error=error),
        )
    except Exception as exc:
        print(f"  [{job_id}] FAILED to write package manifest: {exc}")
        manifest = {
            "package_status": "failed",
            "has_visuals": False,
            "has_voiceover": False,
            "has_clips": False,
            "has_final_video": False,
            "verified_clips": 0,
        }
    try:
        update_job(
            job_id,
            status="failed",
            error=error,
            package_status=manifest["package_status"],
            has_visuals=manifest["has_visuals"],
            has_voiceover=manifest["has_voiceover"],
            has_clips=manifest["has_clips"],
            has_final_video=manifest["has_final_video"],
            verified_clips=manifest["verified_clips"],
        )
    except Exception as exc:
        print(f"  [{job_id}] FAILED to persist job failure state: {exc}")


def _manifest_job_snapshot(job_id: str, fallback: dict, **overrides) -> dict:
    """Build a manifest job snapshot from latest persisted state when available."""
    snapshot = dict(fallback)
    try:
        latest = load_jobs().get(job_id)
        if isinstance(latest, dict):
            snapshot.update(latest)
    except Exception as exc:
        print(f"  [{job_id}] Could not load latest job snapshot for manifest: {exc}")
    snapshot.update(overrides)
    return snapshot


def _generate_visuals(out: Path, storyboard: dict):
    """Generate scene images using the host's image generation tools."""
    prompts_path = out / "visual_prompts.json"
    if not prompts_path.exists():
        return False

    with open(prompts_path) as f:
        manifest = json.load(f)

    visuals_dir = out / "visuals"
    visuals_dir.mkdir(exist_ok=True)

    prompts = manifest.get('prompts', [])
    print(f"  [{out.name}] Would generate {len(prompts)} images (needs image_gen tool access)")

    # Mark which prompts were generated vs pending
    with open(out / "visuals_status.json", 'w') as f:
        json.dump({
            'total': len(prompts),
            'generated': 0,
            'pending': len(prompts),
            'note': 'Images not generated in worker process. Use API directly.'
        }, f, indent=2)
    for path in visuals_dir.glob("scene_*.png"):
        if not path.is_file() or path.stat().st_size <= 8:
            continue
        try:
            if path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n":
                return True
        except OSError:
            continue
    return False


def _generate_voiceover(out: Path, storyboard: dict):
    """Generate voiceover audio."""
    vo_script = out / "audio" / "voiceover_script.txt"
    if not vo_script.exists():
        return False

    with open(vo_script) as f:
        text = f.read()

    print(f"  [{out.name}] Would generate voiceover ({len(text)} chars — needs TTS API key)")
    # TTS requires API key — produce the script and note it needs rendering
    audio = out / "audio" / "voiceover.mp3"
    return audio.is_file() and audio.stat().st_size > 0


def _generate_thumbnail_for_job(out: Path, brief_json: Path):
    """Generate thumbnail prompt for a job."""
    with open(brief_json) as f:
        brief = json.load(f)
    sb_path = out / "storyboard.json"
    with open(sb_path) as f:
        sb = json.load(f)
    topic = brief.get('topic', '')
    tone = brief.get('tone', 'professional')
    dur = sb.get('total_duration', 60)
    hooks = {
        'educational': f"The TRUTH About {topic[:35]}",
        'professional': f"{topic[:35]} — {int(dur)}s",
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
    with open(tp, 'w') as f:
        json.dump({'title_overlay': overlay, 'prompt': prompt, 'filename': 'youtube_thumbnail.png'}, f, indent=2)


def main():
    print("Solo Studio Worker — watching for queued jobs...")
    print(f"  Jobs file: {JOBS_FILE}")

    while True:
        try:
            jobs = load_jobs()
        except (ValueError, OSError) as exc:
            print(f"Jobs store invalid; refusing to process queued jobs until repaired: {exc}")
            time.sleep(POLL_INTERVAL)
            continue
        queued = [j for j in jobs.values() if j.get('status') == 'queued']

        for job in queued:
            job_id = job['id']
            print(f"\n{'='*50}")
            print(f"  Processing job: {job_id}")
            print(f"  Topic: {job['topic'][:80]}")
            print(f"  Duration: {job.get('duration_seconds', 0)}s")
            print(f"{'='*50}")
            try:
                process_job(job_id, job)
            except Exception as exc:
                print(f"  [{job_id}] Unhandled job processing error; worker will continue: {exc}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
