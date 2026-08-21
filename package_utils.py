"""Artifact-derived package status helpers for Solo Studio.

These helpers make output honesty explicit: a job is not a final video just
because the pipeline process completed. Status is derived from files that
actually exist in the job output directory.
"""
from __future__ import annotations

import json
import math
import os
import stat
import re
import shutil
import subprocess
import tempfile
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


STATUS_FAILED = "failed"
STATUS_RESEARCH_ONLY = "research_only"
STATUS_SCRIPT_PACKAGE = "script_package"
STATUS_PROMPT_PACKAGE_ONLY = "prompt_package_only"
STATUS_EDITOR_PACKAGE = "editor_package"
STATUS_CLIPS_GENERATED = "clips_generated"
STATUS_FINAL_VIDEO_READY = "final_video_ready"
STATUS_NONE = "not_started"


EDITOR_PACKAGE_FILES = [
    "creative_brief.json",
    "script.txt",
    "storyboard.json",
    "video_prompts.json",
    "audio/voiceover_script.txt",
    "music_prompt.txt",
    "captions.srt",
    "assembly_manifest.json",
    "timeline.fcpxml",
]

GENERATED_ARTIFACT_PATHS = [
    "creative_brief.json",
    "script.txt",
    "storyboard.json",
    "visual_prompts.json",
    "visuals_status.json",
    "video_prompts.json",
    "music_prompt.txt",
    "captions.srt",
    "assembly_manifest.json",
    "timeline.fcpxml",
    "thumbnail_prompt.json",
    "package_manifest.json",
    "visuals",
    "audio",
    "clips",
    "final",
]


def clear_generated_artifacts(root: str | Path) -> list[str]:
    """Remove generated outputs before a run so stale artifacts cannot be reused."""
    root = Path(root)
    removed: list[str] = []
    for relative in GENERATED_ARTIFACT_PATHS:
        path = root / relative
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
                removed.append(relative)
            elif path.is_dir():
                shutil.rmtree(path)
                removed.append(relative)
        except FileNotFoundError:
            continue
    return removed


def atomic_write_json(path: str | Path, payload: Any) -> None:
    """Atomically write JSON to avoid torn reads of jobs/package metadata."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None
    if path.name == "jobs.json":
        existing_mode = 0o660
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.flush()
        if existing_mode is not None:
            os.chmod(tmp_path, existing_mode)
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def read_json_object(path: str | Path) -> dict:
    """Read a JSON object under the same advisory lock used for writes.

    Missing or empty files mean "no records yet". Corrupt JSON or a non-object
    top-level value is not safe to treat as empty state because that masks data
    loss; callers should surface the ValueError loudly.
    """
    path = Path(path)
    lock_path = path.with_name(f"{path.name}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH)
        try:
            if not path.exists():
                return {}
            content = path.read_text().strip()
            if not content:
                return {}
            try:
                loaded = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ValueError(f"Expected JSON object in {path}, got {type(loaded).__name__}")
            return loaded
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def update_json_file(path: str | Path, updater: Callable[[dict], dict | None]) -> dict:
    """Load/mutate/write a JSON object under an advisory file lock.

    API and worker run as separate processes and both write jobs.json. A plain
    load-modify-write can lose updates when those processes overlap. This helper
    serializes the read/write transaction while retaining atomic replacement for
    readers that are not taking the lock.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            payload: dict[str, Any] = {}
            if path.exists():
                content = path.read_text().strip()
                if content:
                    try:
                        loaded = json.loads(content)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"Refusing to overwrite invalid JSON in {path}: {exc}") from exc
                    if not isinstance(loaded, dict):
                        raise ValueError(
                            f"Refusing to overwrite non-object JSON in {path}: {type(loaded).__name__}"
                        )
                    payload = loaded

            updated = updater(payload)
            if updated is not None:
                payload = updated
            atomic_write_json(path, payload)
            return payload
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _has_file(root: Path, relative: str) -> bool:
    path = root / relative
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _probe_media(path: Path) -> dict[str, Any]:
    """Return conservative media-file verification details."""
    result: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "size_bytes": 0,
        "ffprobe_checked": False,
        "valid": False,
    }
    try:
        file_stat = path.stat()
        result["exists"] = stat.S_ISREG(file_stat.st_mode)
        result["size_bytes"] = file_stat.st_size if result["exists"] else 0
    except OSError as exc:
        result["error"] = f"media file is unavailable: {exc.__class__.__name__}"
        return result
    if not result["exists"] or result["size_bytes"] <= 0:
        return result

    is_mp4 = path.suffix.lower() == ".mp4"
    if is_mp4:
        try:
            signature = path.read_bytes()[:12]
        except OSError as exc:
            result["error"] = f"could not read MP4 signature: {exc}"
            return result
        if len(signature) < 8 or signature[4:8] != b"ftyp":
            result["error"] = "invalid MP4 signature"
            return result

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        result["error"] = "ffprobe unavailable"
        return result

    result["ffprobe_checked"] = True
    probe_command = [ffprobe, "-v", "error"]
    if is_mp4:
        probe_command += [
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type:format=duration",
            "-of", "json",
        ]
    else:
        probe_command += [
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type:format=duration",
            "-of", "json",
        ]
    probe_command.append(str(path))
    try:
        proc = subprocess.run(
            probe_command,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        result["error"] = "ffprobe timed out"
        return result
    except OSError as exc:
        result["error"] = f"ffprobe failed: {exc}"
        return result
    if proc.returncode != 0:
        result["error"] = (proc.stderr or proc.stdout).strip()[:500]
        return result
    if is_mp4:
        try:
            payload = json.loads(proc.stdout)
            if not isinstance(payload, dict):
                raise ValueError("ffprobe payload must be an object")
            streams = payload.get("streams", [])
            media_format = payload.get("format")
            if not isinstance(streams, list) or not isinstance(media_format, dict):
                raise ValueError("ffprobe payload has invalid stream or format data")
            duration = float(media_format.get("duration", "nan"))
        except (AttributeError, ValueError, TypeError, json.JSONDecodeError):
            result["error"] = "invalid MP4 ffprobe output"
            return result
        if not any(stream.get("codec_type") == "video" for stream in streams if isinstance(stream, dict)):
            result["error"] = "MP4 has no video stream"
            return result
        if not math.isfinite(duration):
            result["error"] = "MP4 duration is not finite"
            return result
    else:
        try:
            payload = json.loads(proc.stdout)
            if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list) or not isinstance(payload.get("format"), dict):
                raise ValueError("invalid audio ffprobe output")
            if not any(stream.get("codec_type") == "audio" for stream in payload["streams"] if isinstance(stream, dict)):
                result["error"] = "audio artifact has no audio stream"
                return result
            duration = float(payload["format"].get("duration", "nan"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result["error"] = "invalid audio ffprobe output"
            return result
        if not math.isfinite(duration):
            result["error"] = "media duration is not finite"
            return result
    result["duration_seconds"] = duration
    result["valid"] = duration > 0
    return result


def _storyboard_info(root: Path) -> dict[str, Any]:
    sb = root / "storyboard.json"
    result: dict[str, Any] = {"expected_scenes": 0, "errors": [], "valid": False}
    if not sb.is_file():
        return result
    try:
        data = json.loads(sb.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        result["errors"].append(f"storyboard.json invalid or unreadable: {exc}")
        return result
    if not isinstance(data, dict):
        result["errors"].append(f"storyboard.json must be an object, got {type(data).__name__}")
        return result
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        result["errors"].append(
            f"storyboard.json scenes must be a non-empty list, got {type(scenes).__name__}"
        )
        return result
    result["expected_scenes"] = len(scenes)
    if any(not isinstance(scene, dict) for scene in scenes):
        result["errors"].append("storyboard.json every scene must be an object.")
        return result
    explicit_numbers = [scene.get("scene_number") for scene in scenes if "scene_number" in scene]
    if explicit_numbers and (
        len(explicit_numbers) != len(scenes)
        or any(isinstance(number, bool) or not isinstance(number, int) or number <= 0 for number in explicit_numbers)
        or len(set(explicit_numbers)) != len(explicit_numbers)
        or set(explicit_numbers) != set(range(1, len(scenes) + 1))
    ):
        result["errors"].append("storyboard.json scene numbers must be contiguous, unique, and positive.")
        return result
    result["valid"] = True
    return result


def _expected_scene_count(root: Path) -> int:
    return int(_storyboard_info(root)["expected_scenes"])


def _valid_clip_files(root: Path) -> list[dict[str, Any]]:
    clips_dir = root / "clips"
    if not clips_dir.is_dir():
        return []
    verified = []
    for path in sorted(clips_dir.glob("scene_*.mp4")):
        match = re.fullmatch(r"scene_(\d+)\.mp4", path.name)
        if not match:
            continue
        info = _probe_media(path)
        if info.get("valid"):
            info["scene_number"] = int(match.group(1))
            verified.append(info)
    return verified


def _clip_file_coverage(root: Path) -> tuple[set[int], set[int], list[str]]:
    """Inspect every scene clip filename, including invalid artifacts."""
    clips_dir = root / "clips"
    scene_numbers: list[int] = []
    invalid_files: list[str] = []
    if not clips_dir.is_dir():
        return set(), set(), []
    for path in sorted(clips_dir.iterdir()):
        if not path.is_file():
            continue
        match = re.fullmatch(r"scene_(\d+)\.mp4", path.name)
        if not match:
            invalid_files.append(path.name)
            continue
        scene_numbers.append(int(match.group(1)))
    duplicates = {number for number in scene_numbers if scene_numbers.count(number) > 1}
    return set(scene_numbers), duplicates, invalid_files


def _has_visual_files(root: Path) -> bool:
    visuals_dir = root / "visuals"
    if not visuals_dir.is_dir():
        return False
    for path in visuals_dir.glob("scene_*.png"):
        try:
            if not path.is_file() or path.stat().st_size <= 8:
                continue
            if path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n":
                return True
        except OSError:
            continue
    return False


def compute_package_status(root: str | Path, job_status: str | None = None) -> dict[str, Any]:
    """Compute package status from actual output artifacts.

    Returns a JSON-serializable summary. The status field is intentionally more
    precise than job.status; `completed` can still mean only an editor package.
    """
    root = Path(root)
    storyboard = _storyboard_info(root)
    expected_scenes = int(storyboard["expected_scenes"])
    valid_clips = _valid_clip_files(root)
    clip_scene_numbers = {clip["scene_number"] for clip in valid_clips if isinstance(clip.get("scene_number"), int)}
    all_clip_scene_numbers, duplicate_clip_scene_numbers, invalid_clip_files = _clip_file_coverage(root)
    expected_clip_numbers = set(range(1, expected_scenes + 1))
    missing_clip_numbers = sorted(expected_clip_numbers - clip_scene_numbers)
    extra_clip_numbers = sorted(all_clip_scene_numbers - expected_clip_numbers)
    complete_clip_coverage = (
        bool(expected_scenes)
        and not missing_clip_numbers
        and not extra_clip_numbers
        and not duplicate_clip_scene_numbers
        and not invalid_clip_files
        and len(valid_clips) == expected_scenes
    )
    final_video = _probe_media(root / "final" / "video.mp4")

    artifacts = {
        "creative_brief": _has_file(root, "creative_brief.json"),
        "script": _has_file(root, "script.txt"),
        "storyboard": _has_file(root, "storyboard.json") and bool(storyboard["valid"]),
        "visual_prompts": _has_file(root, "visual_prompts.json"),
        "video_prompts": _has_file(root, "video_prompts.json"),
        "voiceover_script": _has_file(root, "audio/voiceover_script.txt"),
        "voiceover_audio": _probe_media(root / "audio" / "voiceover.mp3").get("valid", False),
        "music_prompt": _has_file(root, "music_prompt.txt"),
        "captions": _has_file(root, "captions.srt"),
        "assembly_manifest": _has_file(root, "assembly_manifest.json"),
        "timeline_fcpxml": _has_file(root, "timeline.fcpxml"),
        "generation_plan": _has_file(root, "clips/generation_plan.json"),
        "clips": len(valid_clips),
        "final_video": final_video.get("valid", False),
    }

    if job_status == STATUS_FAILED:
        package_status = STATUS_FAILED
    elif artifacts["final_video"]:
        package_status = STATUS_FINAL_VIDEO_READY
    elif complete_clip_coverage:
        package_status = STATUS_CLIPS_GENERATED
    elif all(
        artifacts[key]
        for key in (
            "creative_brief",
            "script",
            "storyboard",
            "video_prompts",
            "voiceover_script",
            "music_prompt",
            "captions",
            "assembly_manifest",
            "timeline_fcpxml",
        )
    ):
        package_status = STATUS_EDITOR_PACKAGE
    elif artifacts["video_prompts"]:
        package_status = STATUS_PROMPT_PACKAGE_ONLY
    elif artifacts["script"] and artifacts["storyboard"]:
        package_status = STATUS_SCRIPT_PACKAGE
    elif artifacts["creative_brief"]:
        package_status = STATUS_RESEARCH_ONLY
    else:
        package_status = STATUS_NONE

    return {
        "package_status": package_status,
        "expected_scenes": expected_scenes,
        "verified_clips": len(valid_clips),
        "verified_clip_scene_numbers": sorted(clip_scene_numbers),
        "missing_clip_scene_numbers": missing_clip_numbers,
        "extra_clip_scene_numbers": extra_clip_numbers,
        "duplicate_clip_scene_numbers": sorted(duplicate_clip_scene_numbers),
        "invalid_clip_files": invalid_clip_files,
        "has_partial_clips": bool(valid_clips),
        "has_visuals": _has_visual_files(root),
        "has_voiceover": bool(artifacts["voiceover_audio"]),
        "has_clips": complete_clip_coverage,
        "has_final_video": bool(artifacts["final_video"]),
        "artifacts": artifacts,
        "artifact_errors": storyboard["errors"],
        "final_video_probe": final_video,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def write_package_manifest(root: str | Path, job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write package_manifest.json and return its contents."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    summary = compute_package_status(root, (job or {}).get("status"))
    job_snapshot = dict(job or {})
    job_snapshot.update({
        "package_status": summary["package_status"],
        "has_visuals": summary["has_visuals"],
        "has_voiceover": summary["has_voiceover"],
        "has_clips": summary["has_clips"],
        "has_final_video": summary["has_final_video"],
        "verified_clips": summary["verified_clips"],
        "expected_scenes": summary["expected_scenes"],
    })
    manifest = {
        "job": job_snapshot,
        **summary,
        "status_note": _status_note(summary["package_status"]),
    }
    atomic_write_json(root / "package_manifest.json", manifest)
    return manifest


def _status_note(package_status: str) -> str:
    notes = {
        STATUS_NONE: "No usable package artifacts exist yet.",
        STATUS_RESEARCH_ONLY: "Research/creative brief exists, but script and video package are incomplete.",
        STATUS_SCRIPT_PACKAGE: "Script and storyboard exist, but video prompts/editor package are incomplete.",
        STATUS_PROMPT_PACKAGE_ONLY: "Video prompts exist, but editor timeline and verified clips/final MP4 do not.",
        STATUS_EDITOR_PACKAGE: "Editor-ready package exists; real generated clips/final MP4 are not verified.",
        STATUS_CLIPS_GENERATED: "Scene clips are verified; final assembled MP4 is not verified.",
        STATUS_FINAL_VIDEO_READY: "Final MP4 is verified and ready.",
        STATUS_FAILED: "Job failed; partial artifacts may still be present.",
    }
    return notes.get(package_status, "Unknown package status.")
