"""
Solo Studio Worker — Background pipeline executor.

Watches jobs.json for queued jobs, runs the pipeline, updates status.
Uses a simple polling loop (no Redis dependency — standalone).
"""
import hashlib, json, math, os, re, signal, subprocess, sys, tempfile, threading, time, shutil
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable

import job_store
from audio_generation import AudioGenerationError, generate_voiceover
from media_assembly import MediaError, PublicationRequest, assemble_verified_clips, publish_verified_output, unpublish_verified_output
from package_utils import (
    _open_directory_no_follow,
    _open_regular_descriptor,
    _probe_media,
    _read_prefix_no_follow,
    _sha256_regular_file,
    atomic_write_json,
    atomic_write_text,
    clear_generated_artifacts,
    read_json_artifact,
    read_json_object,
    read_text_artifact,
    resolve_current_attempt_output_dir,
    update_json_file,
    write_package_manifest,
    remove_matching_files,
)

APP_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = APP_DIR / "output"
JOBS_FILE = Path(os.getenv("SOLO_STUDIO_JOBS_FILE", str(APP_DIR / "jobs.json")))
DATABASE_FILE = Path(os.getenv("SOLO_STUDIO_DATABASE_FILE", str(APP_DIR / "state" / "solo_studio.sqlite3")))
DATABASE_CONFIGURED = bool(os.getenv("SOLO_STUDIO_DATABASE_FILE", "").strip())
ENGINES_DIR = APP_DIR / "engines"
PIPELINE = APP_DIR / "pipeline.py"

POLL_INTERVAL = 2  # seconds
LEASE_SECONDS = int(os.getenv("SOLO_STUDIO_WORKER_LEASE_SECONDS", "900"))
WORKER_ID = os.getenv("HOSTNAME", "solo-worker") + ":" + str(os.getpid())
CURRENT_WORKER_ID: str | None = None


def _safe_error(value: object) -> str:
    text = re.sub(r"https?://\S+", "[provider-url-redacted]", str(value or ""))
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password)=\S+", r"\1=[redacted]", text)
    return text[:1000]


def _write_failure_reconciliation_marker(job_id: str, out: Path, job: dict, error: object) -> None:
    """Leave durable local evidence when the job store cannot finalize failure."""
    marker = {
        "version": 1,
        "kind": "failure_reconciliation_required",
        "job_id": job_id,
        "run_id": job.get("run_id"),
        "attempt": job.get("attempt"),
        "status": "reconciliation_required",
        "error": _safe_error(error),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        directory_fd = _open_directory_no_follow(out, create=True)
        os.close(directory_fd)
        atomic_write_json(out / "failure_reconciliation.json", marker)
    except Exception as exc:
        raise RuntimeError(f"could not persist failure reconciliation marker: {exc}") from exc


def load_jobs() -> dict:
    if DATABASE_CONFIGURED:
        return {job["id"]: job for job in job_store.list_jobs(path=DATABASE_FILE)}
    jobs = read_json_object(JOBS_FILE)
    if not isinstance(jobs, dict):
        raise ValueError("legacy jobs store must contain an object")
    for key, job in jobs.items():
        if not isinstance(key, str) or not isinstance(job, dict):
            raise ValueError("legacy jobs store entries must be objects")
        if not isinstance(job.get("id"), str) or job_store._validate_job_id(job["id"]) != job_store._validate_job_id(key):
            raise ValueError("legacy jobs store entries must contain a matching id")
    return jobs


def update_job(job_id: str, **kwargs):
    """Update a job's fields and persist."""
    if DATABASE_CONFIGURED:
        translated = dict(kwargs)
        if "stage" in translated:
            translated["current_stage"] = translated.pop("stage")
        if "error" in translated:
            translated["error_message"] = translated.pop("error")
        job_store.update_fields(job_id, translated, worker_id=CURRENT_WORKER_ID, path=DATABASE_FILE)
        return

    def apply_update(jobs: dict) -> dict:
        if job_id in jobs:
            if not isinstance(jobs[job_id], dict):
                raise ValueError("legacy jobs store entries must be objects")
            if jobs[job_id].get("status") == "cancelled":
                raise job_store.LeaseLost(f"job {job_id} was cancelled")
            jobs[job_id].update(kwargs)
        return jobs

    update_json_file(JOBS_FILE, apply_update)


def _job_is_cancelled(job_id: str) -> bool:
    if DATABASE_CONFIGURED:
        return job_store.is_cancelled(job_id, path=DATABASE_FILE)
    try:
        return load_jobs().get(job_id, {}).get("status") == "cancelled"
    except (OSError, ValueError):
        # Invalid legacy state must never make cancellation checks fail open.
        return True


def _fence_lease(job_id: str) -> None:
    """Renew and verify ownership before/after non-transactional work."""
    if DATABASE_CONFIGURED:
        job_store.heartbeat(
            job_id,
            CURRENT_WORKER_ID or WORKER_ID,
            lease_seconds=LEASE_SECONDS,
            path=DATABASE_FILE,
        )


def _run_with_lease_heartbeat(job_id: str, operation: Callable[[], object]) -> object:
    """Run artifact-producing work while continuously renewing its lease."""
    if not DATABASE_CONFIGURED:
        return operation()
    stop = threading.Event()
    lost = threading.Event()

    def renew() -> None:
        while not stop.wait(max(1.0, min(30.0, LEASE_SECONDS / 3))):
            try:
                _fence_lease(job_id)
            except job_store.JobStoreError as exc:
                lost.set()
                print(f"  [{job_id}] Lease heartbeat failed: {exc}", file=sys.stderr)
                return

    thread = threading.Thread(target=renew, name=f"artifact-lease-{job_id}", daemon=True)
    thread.start()
    try:
        try:
            result = operation()
        except Exception:
            if lost.is_set():
                raise job_store.LeaseLost("artifact operation lost lease ownership")
            raise
        if lost.is_set():
            raise job_store.LeaseLost("artifact operation lost lease ownership")
        return result
    finally:
        stop.set()
        thread.join(timeout=2.0)


def _artifact_digest(path: Path) -> str:
    descriptor = _open_regular_descriptor(path)
    try:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _run_provenance_path(output_root: Path) -> Path:
    return output_root / "run_provenance.json"


def _source_digest(output_root: Path) -> str:
    return _artifact_digest(output_root / "brief.yaml")


def _ensure_run_provenance(output_root: Path, job_id: str, attempt: int | None = None, run_id: str | None = None) -> dict:
    """Create or validate the immutable identity for a resumable job run."""
    source_digest = _source_digest(output_root)
    path = _run_provenance_path(output_root)
    if path.exists():
        provenance = read_json_artifact(path)
        if (
            not isinstance(provenance, dict)
            or provenance.get("job_id") != job_id
            or (attempt is not None and provenance.get("attempt") != attempt)
            or (run_id is not None and provenance.get("run_id") != run_id)
            or provenance.get("source_digest") != source_digest
        ):
            raise ValueError("run provenance does not match this job and source brief")
        return provenance
    provenance = {
        "version": 1,
        "job_id": job_id,
        "attempt": attempt,
        "run_id": run_id,
        "source_digest": source_digest,
        "stages": {},
    }
    atomic_write_json(path, provenance)
    return provenance


def _record_stage_provenance(
    output_root: Path,
    job_id: str,
    stage_key: str,
    required: tuple[str, ...],
) -> None:
    current = job_store.get_job(job_id, path=DATABASE_FILE) if DATABASE_CONFIGURED else None
    provenance = _ensure_run_provenance(
        output_root,
        job_id,
        current.get("attempt") if isinstance(current, dict) else None,
        current.get("run_id") if isinstance(current, dict) else None,
    )
    files = {}
    for relative in required:
        path = output_root / relative
        files[relative] = _artifact_digest(path)
    provenance.setdefault("stages", {})[stage_key] = {
        "files": files,
        "inputs": _stage_input_digests(output_root, stage_key),
    }
    atomic_write_json(_run_provenance_path(output_root), provenance)


def _provenance_matches(
    output_root: Path,
    job_id: str,
    stage_key: str,
    required: tuple[str, ...],
) -> bool:
    try:
        provenance = read_json_artifact(_run_provenance_path(output_root))
        current = job_store.get_job(job_id, path=DATABASE_FILE) if DATABASE_CONFIGURED else None
        if (
            not isinstance(provenance, dict)
            or provenance.get("job_id") != job_id
            or (
                DATABASE_CONFIGURED
                and isinstance(current, dict)
                and (
                    provenance.get("attempt") != current.get("attempt")
                    or provenance.get("run_id") != current.get("run_id")
                )
            )
            or provenance.get("source_digest") != _source_digest(output_root)
        ):
            return False
        recorded = provenance.get("stages", {}).get(stage_key, {}).get("files", {})
        recorded_inputs = provenance.get("stages", {}).get(stage_key, {}).get("inputs", {})
        if not isinstance(recorded_inputs, dict) or recorded_inputs != _stage_input_digests(output_root, stage_key):
            return False
        if not isinstance(recorded, dict) or not all(
            isinstance(recorded.get(relative), str)
            and recorded[relative] == _artifact_digest(output_root / relative)
            for relative in required
        ):
            return False
        if stage_key == "video_generation":
            storyboard = read_json_artifact(output_root / "storyboard.json")
            prompts = read_json_artifact(output_root / "video_prompts.json")
            plan = read_json_artifact(output_root / "clips/generation_plan.json")
            storyboard_scenes = storyboard.get("scenes") if isinstance(storyboard, dict) else None
            prompt_scenes = prompts.get("scenes") if isinstance(prompts, dict) else None
            scenes = plan.get("scenes") if isinstance(plan, dict) else None
            if (
                not isinstance(storyboard_scenes, list)
                or not isinstance(prompt_scenes, list)
                or not isinstance(scenes, list)
                or not scenes
                or [scene.get("scene_number") for scene in storyboard_scenes if isinstance(scene, dict)]
                != [scene.get("scene_number") for scene in prompt_scenes if isinstance(scene, dict)]
                or [scene.get("scene_number") for scene in prompt_scenes if isinstance(scene, dict)]
                != [scene.get("scene_number") for scene in scenes if isinstance(scene, dict)]
            ):
                return False
            if any(not isinstance(scene, dict) for scene in scenes):
                return False
            expected_clip_names = {f"scene_{scene['scene_number']:02d}.mp4" for scene in scenes}
            try:
                clip_directory_entries = set(os.listdir(output_root / "clips"))
                clip_names = {
                    name for name in clip_directory_entries
                    if re.fullmatch(r"scene_[0-9]+\.mp4", name)
                }
                unexpected_media = {
                    name for name in clip_directory_entries
                    if name.lower().endswith(".mp4") and name not in clip_names
                }
            except OSError:
                return False
            if unexpected_media or clip_names != expected_clip_names:
                return False
            for scene in scenes:
                if not isinstance(scene, dict) or scene.get("status") not in {"downloaded", "verified"}:
                    return False
                number = scene.get("scene_number")
                expected_hash = scene.get("sha256")
                if isinstance(number, bool) or not isinstance(number, int) or number <= 0 or not isinstance(expected_hash, str):
                    return False
                clip_path = output_root / "clips" / f"scene_{number:02d}.mp4"
                if _artifact_digest(clip_path) != expected_hash:
                    return False
        return True
    except (OSError, TypeError, ValueError, KeyError, job_store.JobStoreError):
        return False


def _stage_key(name: str) -> str:
    return {
        "Research": "research",
        "Script": "script",
        "Visuals": "visuals",
        "Production": "video_prompts",
        "Video Generation Plan": "video_generation",
        "Editing": "editing",
        "Editor Export": "editor_export",
    }.get(name, name.strip().lower().replace(" ", "_"))


def _cancelled_cleanup(job_id: str, output_root: Path) -> bool:
    """Remove generated artifacts when cancellation wins the terminal race."""
    if not _job_is_cancelled(job_id):
        _fence_lease(job_id)
        return False
    clear_generated_artifacts(output_root)
    print(f"  [{job_id}] CANCELLED; generated artifacts discarded")
    return True


STAGE_REQUIRED_FILES = {
    "research": ("creative_brief.json",),
    "script": ("script.txt", "storyboard.json"),
    "visuals": ("visual_prompts.json",),
    "video_prompts": ("video_prompts.json", "music_prompt.txt", "audio/voiceover_script.txt"),
    "video_generation": ("storyboard.json", "video_prompts.json", "clips/generation_plan.json"),
    "editing": ("captions.srt", "assembly_manifest.json"),
    "editor_export": ("timeline.fcpxml",),
}


STAGE_INPUT_FILES = {
    "research": (),
    "script": ("creative_brief.json",),
    "visuals": ("storyboard.json",),
    "video_prompts": ("storyboard.json",),
    "video_generation": ("storyboard.json", "video_prompts.json"),
    "editing": ("storyboard.json", "clips/generation_plan.json"),
    "editor_export": ("assembly_manifest.json", "captions.srt"),
}


def _stage_input_digests(output_root: Path, stage_key: str) -> dict[str, str]:
    return {
        relative: _artifact_digest(output_root / relative)
        for relative in STAGE_INPUT_FILES.get(stage_key, ())
    }



def _valid_scene_records(records: object, *, number_key: str) -> bool:
    if not isinstance(records, list) or not records:
        return False
    numbers: list[int] = []
    for record in records:
        if not isinstance(record, dict) or record.get("status") == "failed":
            return False
        number = record.get(number_key)
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            return False
        numbers.append(number)
    return numbers == list(range(1, len(numbers) + 1))


def _resume_artifacts_valid(
    output_root: Path,
    stage_key: str,
    required: tuple[str, ...],
    job_id: str | None = None,
) -> bool:
    """Validate resumed stage artifacts instead of trusting file presence alone."""
    if job_id is not None and not _provenance_matches(output_root, job_id, stage_key, required):
        return False
    for relative in required:
        path = output_root / relative
        try:
            if not _read_prefix_no_follow(path, 1):
                return False
            if path.suffix.lower() != ".json":
                continue
            payload = read_json_artifact(path)
        except (OSError, ValueError, TypeError):
            return False
        if not payload or payload.get("status") == "failed":
            return False
        if relative == "storyboard.json":
            if not _valid_scene_records(payload.get("scenes"), number_key="scene_number"):
                return False
        elif relative == "visual_prompts.json":
            prompts = payload.get("prompts")
            if not _valid_scene_records(prompts, number_key="scene_number"):
                return False
        elif relative == "video_prompts.json":
            if not _valid_scene_records(payload.get("scenes"), number_key="scene_number"):
                return False
        elif relative == "clips/generation_plan.json":
            scenes = payload.get("scenes")
            total_scenes = payload.get("total_scenes")
            if (
                payload.get("status") != "completed"
                or not _valid_scene_records(scenes, number_key="scene_number")
                or not isinstance(scenes, list)
                or isinstance(total_scenes, bool)
                or not isinstance(total_scenes, int)
                or total_scenes != len(scenes)
                or any(scene.get("status") not in {"downloaded", "verified"} for scene in scenes)
            ):
                return False
        elif relative == "assembly_manifest.json":
            scenes = payload.get("scenes")
            duration = payload.get("total_duration")
            if (
                not _valid_scene_records(scenes, number_key="scene")
                or isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(float(duration))
                or float(duration) <= 0
            ):
                return False
    return True


def run_stage(job_id: str, name: str, script: str, *args, timeout: int | None = None) -> bool:
    """Run a pipeline stage and update job progress."""
    if _job_is_cancelled(job_id):
        print(f"  [{job_id}] CANCELLED before {name}")
        return False
    stage_key = _stage_key(name)
    output_root = Path(str(args[-1])) if args else None
    required = STAGE_REQUIRED_FILES.get(stage_key, ())
    resume_verified = False
    if DATABASE_CONFIGURED:
        try:
            existing = job_store.get_job(job_id, path=DATABASE_FILE)
            stage_rows = {row.get("stage_name"): row for row in (existing or {}).get("stages", [])}
            if (
                stage_rows.get(stage_key, {}).get("status") == "succeeded"
                and output_root is not None
                and output_root.is_dir()
                and _resume_artifacts_valid(output_root, stage_key, required, job_id)
            ):
                print(f"  [{job_id}] RESUME: verified {name} artifacts already exist")
                resume_verified = True
        except (OSError, job_store.JobStoreError):
            pass
    if resume_verified:
        _fence_lease(job_id)
        return True
    if DATABASE_CONFIGURED:
        try:
            job_store.update_stage(job_id, stage_key, "running", worker_id=CURRENT_WORKER_ID or WORKER_ID, path=DATABASE_FILE)
        except job_store.JobStoreError as exc:
            print(f"  [{job_id}] FAILED to claim stage {name}: {exc}")
            return False
    cmd = [sys.executable, str(ENGINES_DIR / script), *args]
    print(f"  [{job_id}] Running: {name}")
    heartbeat_stop = threading.Event()
    lease_lost = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    if DATABASE_CONFIGURED:
        def renew_lease() -> None:
            while not heartbeat_stop.wait(max(1.0, min(30.0, LEASE_SECONDS / 3))):
                try:
                    job_store.heartbeat(job_id, CURRENT_WORKER_ID or WORKER_ID, lease_seconds=LEASE_SECONDS, path=DATABASE_FILE)
                except job_store.JobStoreError as exc:
                    lease_lost.set()
                    print(f"  [{job_id}] Lease heartbeat failed: {exc}", file=sys.stderr)
                    return
        heartbeat_thread = threading.Thread(target=renew_lease, name=f"lease-{job_id}", daemon=True)
        heartbeat_thread.start()

    def stop_lease_heartbeat() -> None:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2.0)

    def mark(status: str, message: str | None = None) -> None:
        if DATABASE_CONFIGURED:
            try:
                job_store.update_stage(
                    job_id,
                    stage_key,
                    status,
                    worker_id=CURRENT_WORKER_ID or WORKER_ID,
                    error_code="stage_failed" if status == "failed" else None,
                    error_message=message,
                    path=DATABASE_FILE,
                )
            except job_store.JobStoreError as exc:
                print(f"  [{job_id}] FAILED to persist {name} stage state: {exc}")
                raise

    try:
        stage_timeout = timeout or int(os.getenv("SOLO_STUDIO_STAGE_TIMEOUT", "300"))
        if not DATABASE_CONFIGURED:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=stage_timeout)
        else:
            with tempfile.TemporaryFile(mode="w+b") as stdout_log, tempfile.TemporaryFile(mode="w+b") as stderr_log:
                process = subprocess.Popen(
                    cmd,
                    stdout=stdout_log,
                    stderr=stderr_log,
                    start_new_session=True,
                )
                deadline = time.monotonic() + stage_timeout
                while process.poll() is None:
                    if lease_lost.is_set():
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            process.wait(timeout=5)
                        break
                    if time.monotonic() >= deadline:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            process.wait(timeout=5)
                        raise subprocess.TimeoutExpired(cmd, stage_timeout)
                    time.sleep(0.1)
                process.wait()
                stderr_log.seek(0)
                stderr = stderr_log.read(8192).decode("utf-8", errors="replace")
                result = subprocess.CompletedProcess(cmd, process.returncode, "", stderr)
    except subprocess.TimeoutExpired:
        stop_lease_heartbeat()
        print(f"  [{job_id}] FAILED: {name} timed out")
        mark("failed", "stage timeout")
        return False
    except OSError as exc:
        stop_lease_heartbeat()
        print(f"  [{job_id}] FAILED: {name} could not start: {exc}")
        mark("failed", "stage could not start")
        return False
    if lease_lost.is_set():
        stop_lease_heartbeat()
        print(f"  [{job_id}] FAILED: {name} lease ownership was lost")
        mark("failed", "stage lease ownership was lost")
        return False
    if result.returncode != 0:
        stop_lease_heartbeat()
        print(f"  [{job_id}] FAILED: {name}")
        print(f"  stderr: {_safe_error(result.stderr[:500])}")
        mark("failed", f"{name} exited with code {result.returncode}")
        return False
    stop_lease_heartbeat()
    if output_root is not None and required:
        _fence_lease(job_id)
        _record_stage_provenance(output_root, job_id, stage_key, required)
        _fence_lease(job_id)
    mark("succeeded")
    return True


def _prepare_attempt_output(job_id: str, job: dict) -> tuple[Path, str]:
    if DATABASE_CONFIGURED:
        out = resolve_current_attempt_output_dir(OUTPUT_ROOT, job)
        if out is None:
            raise ValueError("claimed SQLite job is missing run_id")
        source = Path(job.get("output_dir") or (OUTPUT_ROOT / job_id)) / "brief.yaml"
    else:
        out = OUTPUT_ROOT / job_id
        source = out / "brief.yaml"
    out_fd = _open_directory_no_follow(out, create=True)
    os.close(out_fd)
    brief = out / "brief.yaml"
    if source.resolve(strict=False) != brief.resolve(strict=False):
        content = read_text_artifact(source)
        atomic_write_text(brief, content)
    source_digest = _source_digest(out)
    return out, source_digest


def process_job(job_id: str, job: dict):
    """Run the full pipeline for a single job."""
    out = OUTPUT_ROOT / job_id

    try:
        _fence_lease(job_id)
        out, source_digest = _prepare_attempt_output(job_id, job)
        # Clear the durable running marker before touching retry artifacts. This
        # prevents an old completed status from being paired with a stale final
        # MP4 while a retry is rebuilding the current run.
        update_job(job_id, status="running", stage="research", progress=0.05, source_digest=source_digest)
        # Every claim gets a clean generated-artifact set.  A reclaimed lease
        # represents a new attempt and must not inherit prior media, manifests,
        # or stage outputs.
        clear_generated_artifacts(out)
        _ensure_run_provenance(out, job_id, job.get("attempt"), job.get("run_id"))

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
        sb = read_json_artifact(sb_path)
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
                _fence_lease(job_id)
                update_job(job_id, has_visuals=_run_with_lease_heartbeat(job_id, lambda: _generate_visuals(out, sb)))
                _fence_lease(job_id)
            except job_store.LeaseLost:
                raise
            except Exception as e:
                print(f"  [{job_id}] Image generation not available: {e}")

        update_job(job_id, stage="voiceover", progress=0.42)

        # Stage 4: Voiceover — generate TTS audio
        try:
            _fence_lease(job_id)
            update_job(job_id, has_voiceover=_run_with_lease_heartbeat(job_id, lambda: _generate_voiceover(out, sb)))
            _fence_lease(job_id)
        except job_store.LeaseLost:
            raise
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
                scene_count = len(read_json_artifact(vp_path).get("scenes", []))
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
            _fence_lease(job_id)
            _run_with_lease_heartbeat(job_id, lambda: _generate_thumbnail_for_job(out, brief_json))
            _fence_lease(job_id)
        except job_store.LeaseLost:
            raise
        except Exception as e:
            print(f"  [{job_id}] Thumbnail: {e}")

        update_job(job_id, stage="assembly", progress=0.92)

        if _cancelled_cleanup(job_id, out):
            return

        # Real provider runs publish a final MP4 only after exact scene coverage
        # and independent ffprobe verification. Dry-run/editor-package jobs do
        # do not fail merely because no provider clips exist.
        try:
            _fence_lease(job_id)
            plan_digest = _artifact_digest(out / "clips" / "generation_plan.json")

            def publish_final(request: PublicationRequest) -> None:
                if not DATABASE_CONFIGURED:
                    publish_verified_output(request)
                    return
                verified = request.verified
                job_store.publish_final_media(
                    job_id,
                    CURRENT_WORKER_ID or WORKER_ID,
                    str(job.get("run_id") or ""),
                    publication=lambda: publish_verified_output(request),
                    rollback_publication=lambda: unpublish_verified_output(request),
                    evidence={
                        "final_video_sha256": verified["sha256"],
                        "final_video_duration_seconds": verified["duration_seconds"],
                        "final_video_plan_sha256": plan_digest,
                    },
                    path=DATABASE_FILE,
                )

            final_media = _assemble_verified_output(
                out,
                sb,
                lease_check=lambda: _fence_lease(job_id),
                publication_callback=publish_final if DATABASE_CONFIGURED else None,
            )
            if not DATABASE_CONFIGURED:
                _fence_lease(job_id)
            # SQLite publication already durably records the descriptor-bound
            # final-media evidence inside its transaction.  A second update
            # here would be outside that barrier and previously rewrote the
            # hash obtained from a post-publication pathname reopen.
            if final_media and not DATABASE_CONFIGURED:
                update_job(
                    job_id,
                    final_video_sha256=final_media["sha256"],
                    final_video_plan_sha256=plan_digest,
                    final_video_duration_seconds=final_media["duration_seconds"],
                )
        except MediaError as exc:
            _fail_job(job_id, out, job, f"Final media assembly failed: {exc}")
            return

        completed_at = datetime.now(timezone.utc).isoformat()
        try:
            if _cancelled_cleanup(job_id, out):
                return
            _fence_lease(job_id)
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
            _fence_lease(job_id)
            if _cancelled_cleanup(job_id, out):
                return
            _fence_lease(job_id)
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
        except job_store.LeaseLost:
            if _cancelled_cleanup(job_id, out):
                return
            raise
        print(f"  [{job_id}] COMPLETED — {sb.get('total_duration', 0):.0f}s, {len(sb.get('scenes', []))} scenes")

    except Exception as e:
        print(f"  [{job_id}] FAILED: {e}")
        try:
            _fail_job(job_id, out, job, str(e))
        except Exception as finalizer_exc:
            print(f"  [{job_id}] FAILED to finalize job failure: {finalizer_exc}")
            try:
                _write_failure_reconciliation_marker(job_id, out, job, finalizer_exc)
            except Exception as marker_exc:
                print(f"  [{job_id}] FAILED to persist failure reconciliation marker: {marker_exc}")
            raise


def _fail_job(job_id: str, out: Path, job: dict, error: str):
    """Persist a failed terminal state plus an artifact-derived manifest."""
    state_unreadable = False
    try:
        cancelled = _job_is_cancelled(job_id)
    except Exception as exc:
        # Unreadable SQLite state is not proof of cancellation. Preserve the
        # failure artifact first; terminal-state persistence will report the
        # store error separately below.
        print(f"  [{job_id}] Could not read cancellation state: {exc}")
        cancelled = False
        state_unreadable = True
    if cancelled:
        if DATABASE_CONFIGURED:
            print(f"  [{job_id}] Cancellation is terminal; preserving cancelled state")
            return
        try:
            load_jobs()
        except (OSError, ValueError):
            # The state is unreadable, not confirmed cancelled; preserve evidence.
            pass
        else:
            print(f"  [{job_id}] Cancellation is terminal; preserving cancelled state")
            return
    safe_error = _safe_error(error)
    if DATABASE_CONFIGURED and not job.get("run_id") and state_unreadable:
        # A legacy caller may have no claimed run identity while the durable
        # store is unreadable. Preserve the historical diagnostic manifest and
        # leave a durable reconciliation marker; do not leave a running job
        # unexplained by simply logging and returning.
        try:
            write_package_manifest(out, _manifest_job_snapshot(job_id, job, status="failed", error=safe_error))
        except Exception as exc:
            print(f"  [{job_id}] FAILED to write package manifest: {exc}")
        _write_failure_reconciliation_marker(job_id, out, job, "job store could not be read while finalizing failure")
        print(f"  [{job_id}] Failure reconciliation marker written; durable job state still requires repair")
        return
    if DATABASE_CONFIGURED:
        def publish_manifest(current: dict) -> dict:
            snapshot = dict(current)
            snapshot.update(status="failed", error=safe_error)
            if not isinstance(snapshot.get("source_digest"), str) or not snapshot["source_digest"]:
                # A failure can occur before the attempt brief is durable. Do
                # not let an incomplete provenance tuple hide the failure
                # evidence behind a misleading ``not_started`` package status.
                snapshot["run_id"] = None
            return write_package_manifest(out, snapshot)

        def cleanup_manifest() -> None:
            remove_matching_files(out, "package_manifest.json")

        try:
            job_store.finalize_failure(
                job_id,
                CURRENT_WORKER_ID or WORKER_ID,
                str(job.get("run_id") or ""),
                publish_manifest=publish_manifest,
                cleanup_manifest=cleanup_manifest,
                error_code="stage_failed",
                error_message=safe_error,
                retryable=os.getenv("SOLO_STUDIO_RETRY_STAGE_FAILURES", "1").strip().lower() in {"1", "true", "yes"},
                path=DATABASE_FILE,
            )
        except Exception as exc:
            _write_failure_reconciliation_marker(job_id, out, job, exc)
            print(f"  [{job_id}] Failure reconciliation marker written; durable job state still requires repair")
            return
        return
    try:
        manifest = write_package_manifest(
            out,
            _manifest_job_snapshot(job_id, job, status="failed", error=safe_error),
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
            error=safe_error,
            package_status=manifest["package_status"],
            has_visuals=manifest["has_visuals"],
            has_voiceover=manifest["has_voiceover"],
            has_clips=manifest["has_clips"],
            has_final_video=manifest["has_final_video"],
            verified_clips=manifest["verified_clips"],
        )
    except Exception as exc:
        _write_failure_reconciliation_marker(job_id, out, job, exc)
        print(f"  [{job_id}] Failure reconciliation marker written; durable job state still requires repair")
        return


def _manifest_job_snapshot(job_id: str, fallback: dict, **overrides) -> dict:
    """Build a manifest job snapshot from latest persisted state when available."""
    snapshot = dict(fallback)
    try:
        if DATABASE_CONFIGURED:
            latest = job_store.get_job(job_id, path=DATABASE_FILE)
        else:
            latest = load_jobs().get(job_id)
        if isinstance(latest, dict):
            snapshot.update(latest)
    except Exception as exc:
        print(f"  [{job_id}] Could not load latest job snapshot for manifest: {exc}")
    snapshot.update(overrides)
    return snapshot


def _assemble_verified_output(
    out: Path,
    storyboard: dict,
    *,
    lease_check: Callable[[], None] | None = None,
    publication_callback: Callable[[PublicationRequest], None] | None = None,
) -> dict | None:
    """Assemble generated scene clips, or return None for dry-run packages."""
    clips_dir = out / "clips"
    plan_path = clips_dir / "generation_plan.json"
    if not plan_path.exists():
        return None
    try:
        plan = read_json_artifact(plan_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaError("generation plan is malformed") from exc
    if not isinstance(plan, dict):
        raise MediaError("generation plan must be an object")
    plan_scenes = plan.get("scenes")
    plan_scene_numbers = (
        [scene.get("scene_number") for scene in plan_scenes]
        if isinstance(plan_scenes, list) and all(isinstance(scene, dict) for scene in plan_scenes)
        else []
    )
    storyboard_scenes = storyboard.get("scenes")
    storyboard_scene_numbers = (
        [scene.get("scene_number") for scene in storyboard_scenes]
        if isinstance(storyboard_scenes, list) and all(isinstance(scene, dict) for scene in storyboard_scenes)
        else []
    )
    if plan.get("status") == "dry_run":
        return None
    if (
        not plan_scenes
        or not storyboard_scenes
        or plan.get("status") != "completed"
        or any(scene.get("status") not in {"downloaded", "verified"} for scene in plan_scenes if isinstance(scene, dict))
        or plan_scene_numbers != storyboard_scene_numbers
        or plan_scene_numbers != list(range(1, len(plan_scene_numbers) + 1))
        or any(
            isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
            for number in plan_scene_numbers
        )
    ):
        raise MediaError("storyboard and generation plan scene identity does not match")
    try:
        clips_fd = _open_directory_no_follow(clips_dir, create=False)
        try:
            clip_names = sorted(os.listdir(clips_fd))
        finally:
            os.close(clips_fd)
    except OSError as exc:
        raise MediaError("clips directory is not a safe regular directory") from exc
    clips = []
    for name in clip_names:
        if name == plan_path.name:
            continue
        match = re.fullmatch(r"scene_([0-9]+)\.mp4", name)
        if match is None or name != f"scene_{int(match.group(1)):02d}.mp4":
            raise MediaError("clips directory contains an invalid or unexpected artifact")
        entry = clips_dir / name
        descriptor = -1
        try:
            descriptor = _open_regular_descriptor(entry)
        except OSError as exc:
            raise MediaError("clips directory contains an invalid or unexpected artifact") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        clips.append(entry)
    if not clips:
        if plan.get("status") == "completed":
            raise MediaError("generation plan is complete but no clips were published")
        return None
    expected = plan.get("total_scenes")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
        raise MediaError("generation plan has no valid scene count")
    if expected != len(plan_scene_numbers):
        raise MediaError("generation plan scene count does not match its scene list")
    expected_names = {f"scene_{number:02d}.mp4" for number in plan_scene_numbers}
    if {clip.name for clip in clips} != expected_names:
        raise MediaError("generated clips do not provide exact scene coverage")
    expected_hashes: dict[int, str] = {}
    for scene in plan_scenes:
        if not isinstance(scene, dict):
            raise MediaError("generation plan contains an invalid scene")
        digest = scene.get("sha256")
        number = scene.get("scene_number")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise MediaError("completed generation plan is missing a valid clip hash")
        expected_hashes[number] = digest
    if set(expected_hashes) != set(plan_scene_numbers):
        raise MediaError("generation plan clip hashes do not cover every scene")
    raw_expected_duration = storyboard.get("total_duration")
    expected_duration: float | None = None
    if isinstance(raw_expected_duration, (int, float)) and not isinstance(raw_expected_duration, bool):
        try:
            candidate = float(raw_expected_duration)
            if math.isfinite(candidate):
                expected_duration = candidate
        except (ValueError, OverflowError):
            pass
    output = out / "final" / "video.mp4"
    return assemble_verified_clips(
        clips,
        output,
        expected_duration=expected_duration,
        lease_check=lease_check,
        publication_callback=publication_callback,
        expected_clip_sha256={
            f"scene_{number:02d}.mp4": digest for number, digest in expected_hashes.items()
        },
    )


def _generate_visuals(out: Path, storyboard: dict):
    """Generate scene images using the host's image generation tools."""
    prompts_path = out / "visual_prompts.json"
    if not prompts_path.exists():
        return False

    manifest = read_json_artifact(prompts_path)

    visuals_dir = out / "visuals"
    visuals_dir.mkdir(exist_ok=True)

    prompts = manifest.get('prompts', [])
    print(f"  [{out.name}] Would generate {len(prompts)} images (needs image_gen tool access)")

    # Mark which prompts were generated vs pending
    atomic_write_json(out / "visuals_status.json", {
            'total': len(prompts),
            'generated': 0,
            'pending': len(prompts),
            'note': 'Images not generated in worker process. Use API directly.'
        })
    for path in visuals_dir.glob("scene_*.png"):
        try:
            if _read_prefix_no_follow(path, 8) == b"\x89PNG\r\n\x1a\n":
                return True
        except OSError:
            continue
    return False


def _generate_voiceover(out: Path, storyboard: dict):
    """Generate voiceover audio."""
    vo_script = out / "audio" / "voiceover_script.txt"
    if not vo_script.exists():
        return False

    text = read_text_artifact(vo_script)

    audio = out / "audio" / "voiceover.mp3"
    if os.getenv("SOLO_STUDIO_ENABLE_TTS", "0").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            metadata = generate_voiceover(text, audio)
            print(f"  [{out.name}] Voiceover generated and verified ({metadata['duration_seconds']:.2f}s)")
            return True
        except AudioGenerationError as exc:
            print(f"  [{out.name}] Voiceover unavailable: {exc}")
            return False
    print(f"  [{out.name}] Would generate voiceover ({len(text)} chars — TTS disabled)")
    return bool(_probe_media(audio).get("valid"))


def _generate_thumbnail_for_job(out: Path, brief_json: Path):
    """Generate thumbnail prompt for a job."""
    brief = read_json_artifact(brief_json)
    sb_path = out / "storyboard.json"
    sb = read_json_artifact(sb_path)
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
    atomic_write_json(tp, {'title_overlay': overlay, 'prompt': prompt, 'filename': 'youtube_thumbnail.png'})


def main():
    global CURRENT_WORKER_ID
    print("Solo Studio Worker — watching for queued jobs...")
    if DATABASE_CONFIGURED:
        try:
            job_store.initialize(DATABASE_FILE)
            job_store.reconcile_expired_leases(path=DATABASE_FILE)
            abandoned = job_store.reconcile_initializing_jobs(path=DATABASE_FILE)
            if abandoned:
                print(f"  Reconciled {abandoned} abandoned initializing job(s)")
            if JOBS_FILE.exists():
                try:
                    job_store.import_jobs_json_once(JOBS_FILE, path=DATABASE_FILE)
                except job_store.InvalidStoreState as exc:
                    # A populated database or malformed legacy file is visible
                    # operational state; never silently replace SQLite data.
                    if "populated" not in str(exc):
                        print(f"JOB_STORE_IMPORT_FAILED: {exc}", file=sys.stderr)
                        return 2
        except job_store.JobStoreError as exc:
            print(f"JOB_STORE_INIT_FAILED: {exc}", file=sys.stderr)
            return 2
        print(f"  Database: {DATABASE_FILE}")
    else:
        print(f"  Jobs file: {JOBS_FILE}")

    while True:
        if DATABASE_CONFIGURED:
            try:
                job_store.record_worker_heartbeat(WORKER_ID, status="idle", pid=os.getpid(), hostname=os.getenv("HOSTNAME"), path=DATABASE_FILE)
                job_store.reconcile_initializing_jobs(path=DATABASE_FILE)
                claim = job_store.claim_next_job(WORKER_ID, lease_seconds=LEASE_SECONDS, path=DATABASE_FILE)
            except job_store.JobStoreError as exc:
                print(f"JOB_STORE_CLAIM_FAILED: {exc}", file=sys.stderr)
                return 2
            if claim is None:
                time.sleep(POLL_INTERVAL)
                continue
            job_id = claim.job["id"]
            print(f"\n{'='*50}")
            print(f"  Processing job: {job_id}")
            print(f"  Topic: {str(claim.job.get('topic', ''))[:80]}")
            print(f"  Duration: {claim.job.get('duration_seconds', 0)}s")
            print(f"{'='*50}")
            CURRENT_WORKER_ID = WORKER_ID
            try:
                job_store.record_worker_heartbeat(WORKER_ID, status="running", pid=os.getpid(), hostname=os.getenv("HOSTNAME"), path=DATABASE_FILE)
                process_job(job_id, claim.job)
            except KeyboardInterrupt:
                raise
            except job_store.JobStoreError as exc:
                print(f"JOB_STORE_UPDATE_FAILED: {exc}", file=sys.stderr)
                return 2
            finally:
                try:
                    job_store.record_worker_heartbeat(WORKER_ID, status="idle", pid=os.getpid(), hostname=os.getenv("HOSTNAME"), path=DATABASE_FILE)
                except job_store.JobStoreError:
                    pass
                CURRENT_WORKER_ID = None
            continue

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
    raise SystemExit(main() or 0)
