"""Artifact-derived package status helpers for Solo Studio.

These helpers make output honesty explicit: a job is not a final video just
because the pipeline process completed. Status is derived from files that
actually exist in the job output directory.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import re
import shutil
import subprocess
import fcntl
import fnmatch
import uuid
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
RUNS_DIRECTORY_NAME = "runs"
LEGACY_FLAT_RUN_ID = "legacy-import"


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
    "run_provenance.json",
    "failure_reconciliation.json",
    "visuals",
    "audio",
    "clips",
    "final",
]


def _validate_path_component(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > 128
        or value.strip() in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be a path-safe non-empty string")
    return value.strip()


def resolve_current_attempt_output_dir(output_root: str | Path, job: dict[str, Any]) -> Path | None:
    """Resolve the current durable attempt directory for a stored job.

    SQLite jobs keep a durable job output root in ``output_dir``. Generated
    artifacts for a claim live below ``output_dir/runs/<run_id>``; until a claim
    assigns ``run_id`` there is intentionally no current package directory.
    """
    if not isinstance(job, dict):
        raise ValueError("job must be an object")
    job_id = _validate_path_component(job.get("id"), "job id")
    run_id = job.get("run_id")
    if run_id is None or run_id == "":
        return None
    run_id = _validate_path_component(run_id, "run id")
    root = Path(output_root).resolve()
    durable_output = Path(job.get("output_dir") or (root / job_id))
    if not durable_output.is_absolute():
        raise ValueError("job output_dir must be absolute")
    if durable_output.exists() and durable_output.is_symlink():
        raise ValueError("job output_dir must not be a symlink")
    durable_resolved = durable_output.resolve(strict=False)
    if durable_resolved.name != job_id or durable_resolved.parent != root:
        raise ValueError("job output_dir must be the path-safe job root under the configured output root")
    if run_id == LEGACY_FLAT_RUN_ID:
        if job.get("status") not in {"completed", "failed", "cancelled"}:
            raise ValueError("legacy flat output is only valid for terminal imported jobs")
        return durable_output
    run_dir = durable_output / RUNS_DIRECTORY_NAME / run_id
    if run_dir.exists() and run_dir.is_symlink():
        raise ValueError("run output directory must not be a symlink")
    run_resolved = run_dir.resolve(strict=False)
    expected_parent = durable_resolved / RUNS_DIRECTORY_NAME
    if run_resolved.name != run_id or run_resolved.parent != expected_parent:
        raise ValueError("run output directory escaped the durable job root")
    return run_dir


def _remove_tree_at(parent_fd: int, name: str) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(name, directory_flags, dir_fd=parent_fd)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        if isinstance(exc, FileNotFoundError):
            return
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        return
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    _remove_tree_at(directory_fd, entry.name)
                else:
                    try:
                        os.unlink(entry.name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(directory_fd)


def clear_generated_artifacts(root: str | Path) -> list[str]:
    """Remove generated outputs using descriptor-relative no-follow traversal."""
    root = Path(root)
    try:
        root_fd = _open_directory_no_follow(root, create=False)
    except FileNotFoundError:
        return []
    except (NotADirectoryError, OSError, ValueError) as exc:
        raise ValueError("output root must be a real directory with no symlinked ancestors") from exc
    removed: list[str] = []
    try:
        for relative in GENERATED_ARTIFACT_PATHS:
            components = relative.split("/")
            parent_fd = root_fd
            opened: list[int] = []
            try:
                for component in components[:-1]:
                    child_fd = os.open(
                        component,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_fd,
                    )
                    opened.append(child_fd)
                    parent_fd = child_fd
                name = components[-1]
                try:
                    _remove_tree_at(parent_fd, name)
                    removed.append(relative)
                except FileNotFoundError:
                    pass
            except FileNotFoundError:
                pass
            except (NotADirectoryError, OSError) as exc:
                raise ValueError(f"could not clear generated artifact {relative}") from exc
            finally:
                for descriptor in reversed(opened):
                    os.close(descriptor)
    finally:
        os.close(root_fd)
    return removed


def remove_matching_files(root: str | Path, pattern: str) -> list[str]:
    """Remove matching regular files without following a directory symlink."""
    root_fd = _open_directory_no_follow(root, create=False)
    removed: list[str] = []
    try:
        for name in os.listdir(root_fd):
            if not fnmatch.fnmatch(name, pattern):
                continue
            try:
                entry_stat = os.lstat(name, dir_fd=root_fd)
                if stat.S_ISREG(entry_stat.st_mode) or stat.S_ISLNK(entry_stat.st_mode):
                    os.unlink(name, dir_fd=root_fd)
                    removed.append(name)
            except FileNotFoundError:
                continue
    finally:
        os.close(root_fd)
    return removed


def _open_directory_no_follow(path: str | Path, *, create: bool = False) -> int:
    """Open every directory component with O_NOFOLLOW, optionally creating it."""
    absolute = Path(path).absolute()
    components = absolute.parts[1:]
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError(f"unsafe directory path: {path}")
    descriptor = os.open(os.sep, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        for component in components:
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o770, dir_fd=descriptor)
                child = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def atomic_write_json(path: str | Path, payload: Any) -> None:
    """Atomically write JSON using a no-follow parent directory descriptor."""
    path = Path(path)
    directory_fd = _open_directory_no_follow(path.parent, create=True)
    temporary_name = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        try:
            existing_stat = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(existing_stat.st_mode) or not stat.S_ISREG(existing_stat.st_mode):
                raise ValueError(f"Refusing to atomically replace non-regular JSON file: {path}")
            existing_mode = stat.S_IMODE(existing_stat.st_mode) & 0o660
            if existing_mode == 0:
                existing_mode = 0o600
        except FileNotFoundError:
            existing_mode = None
        if path.name == "jobs.json":
            existing_mode = 0o660
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            if existing_mode is not None:
                os.fchmod(handle.fileno(), existing_mode)
        os.replace(temporary_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def atomic_write_text(path: str | Path, content: str) -> None:
    """Atomically publish UTF-8 text without following the final path component."""
    path = Path(path)
    directory_fd = _open_directory_no_follow(path.parent, create=True)
    temporary_name = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _read_json_object_unlocked(path: Path) -> dict:
    """Read a regular JSON object through an O_NOFOLLOW descriptor."""
    try:
        descriptor = _open_regular_descriptor(path)
    except FileNotFoundError:
        return {}
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"Expected regular JSON file in {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            content = handle.read().strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not content:
        return {}
    try:
        loaded = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object in {path}, got {type(loaded).__name__}")
    return loaded


def _open_lock_file(path: Path):
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = _open_directory_no_follow(path.parent, create=True)
    try:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Expected regular lock file in {path}")
        return os.fdopen(descriptor, "r+")
    except Exception:
        os.close(descriptor)
        raise


def read_json_object(path: str | Path) -> dict:
    """Read a JSON object under the same advisory lock used for writes."""
    path = Path(path)
    lock_path = path.with_name(f"{path.name}.lock")
    with _open_lock_file(lock_path) as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH)
        try:
            return _read_json_object_unlocked(path)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def read_json_artifact(path: str | Path) -> dict:
    """Read an artifact JSON object without creating a persistent lock file."""
    return _read_json_object_unlocked(Path(path))


def read_text_artifact(path: str | Path) -> str:
    """Read a regular text artifact through a no-follow descriptor."""
    try:
        descriptor = _open_regular_descriptor(path)
    except FileNotFoundError as exc:
        raise ValueError(f"Text artifact is missing: {path}") from exc
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def update_json_file(path: str | Path, updater: Callable[[dict], dict | None]) -> dict:
    """Load/mutate/write a JSON object under an advisory file lock.

    API and worker run as separate processes and both write jobs.json. A plain
    load-modify-write can lose updates when those processes overlap. This helper
    serializes the read/write transaction while retaining atomic replacement for
    readers that are not taking the lock.
    """
    path = Path(path)
    lock_path = path.with_name(f"{path.name}.lock")
    with _open_lock_file(lock_path) as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            try:
                existing_stat = os.lstat(path)
            except FileNotFoundError:
                existing_stat = None
            if existing_stat is not None and (
                stat.S_ISLNK(existing_stat.st_mode) or not stat.S_ISREG(existing_stat.st_mode)
            ):
                raise ValueError(f"Refusing to update non-regular JSON file: {path}")
            payload: dict[str, Any] = {}
            if existing_stat is not None:
                try:
                    payload = _read_json_object_unlocked(path)
                except ValueError as exc:
                    raise ValueError(f"Refusing to overwrite invalid JSON in {path}: {exc}") from exc

            updated = updater(payload)
            if updated is not None:
                payload = updated
            atomic_write_json(path, payload)
            return payload
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _open_regular_under_root(root: Path, path: Path) -> int:
    """Open a contained regular file by walking held no-follow descriptors."""
    root = Path(root)
    path = Path(path)
    relative = path.relative_to(root)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError("invalid rooted artifact path")
    directory_fd = _open_directory_no_follow(root, create=False)
    try:
        current_fd = directory_fd
        owned_directory_fds: list[int] = []
        try:
            for component in relative.parts[:-1]:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
                owned_directory_fds.append(next_fd)
                current_fd = next_fd
            descriptor = os.open(
                relative.parts[-1],
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=current_fd,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise OSError("rooted artifact is not a regular file")
            return descriptor
        finally:
            for owned_fd in reversed(owned_directory_fds):
                os.close(owned_fd)
    finally:
        os.close(directory_fd)


def _safe_regular_file(root: Path, path: Path) -> bool:
    """Accept only regular files whose complete path stays inside ``root``."""
    try:
        descriptor = _open_regular_under_root(Path(root), Path(path))
        os.close(descriptor)
        return True
    except (OSError, ValueError):
        return False


def _has_file(root: Path, relative: str) -> bool:
    path = root / relative
    try:
        descriptor = _open_regular_under_root(root, path)
        try:
            return os.fstat(descriptor).st_size > 0
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return False


def _open_regular_descriptor(path: str | Path) -> int:
    """Open one regular file through no-follow parent descriptors."""
    path = Path(path)
    directory_fd = _open_directory_no_follow(path.parent, create=False)
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(descriptor)
            raise OSError("media artifact is not a regular file")
        return descriptor
    finally:
        os.close(directory_fd)


def _read_prefix_no_follow(path: str | Path, size: int) -> bytes:
    descriptor = _open_regular_descriptor(path)
    try:
        return os.read(descriptor, size)
    finally:
        os.close(descriptor)


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
        descriptor = _open_regular_descriptor(path)
    except OSError as exc:
        result["error"] = f"media file is unavailable: {exc.__class__.__name__}"
        return result
    try:
        file_stat = os.fstat(descriptor)
        result["exists"] = stat.S_ISREG(file_stat.st_mode)
        result["size_bytes"] = file_stat.st_size if result["exists"] else 0
        if not result["exists"] or result["size_bytes"] <= 0:
            return result

        is_mp4 = path.suffix.lower() == ".mp4"
        if is_mp4:
            os.lseek(descriptor, 0, os.SEEK_SET)
            signature = os.read(descriptor, 12)
            if len(signature) < 8 or signature[4:8] != b"ftyp":
                result["error"] = "invalid MP4 signature"
                return result

        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            result["error"] = "ffprobe unavailable"
            return result

        result["ffprobe_checked"] = True
        probe_command = [ffprobe, "-v", "error", "-select_streams", "v:0" if is_mp4 else "a:0",
                         "-show_entries", "stream=codec_type:format=duration", "-of", "json",
                         f"/proc/self/fd/{descriptor}"]
        try:
            proc = subprocess.run(probe_command, capture_output=True, text=True, timeout=15,
                                  pass_fds=(descriptor,))
        except subprocess.TimeoutExpired:
            result["error"] = "ffprobe timed out"
            return result
        except OSError as exc:
            result["error"] = f"ffprobe failed: {exc}"
            return result
        if proc.returncode != 0:
            result["error"] = (proc.stderr or proc.stdout).strip()[:500]
            return result
        try:
            payload = json.loads(proc.stdout)
            if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list) or not isinstance(payload.get("format"), dict):
                raise ValueError("invalid ffprobe output")
            expected_type = "video" if is_mp4 else "audio"
            if not any(stream.get("codec_type") == expected_type for stream in payload["streams"] if isinstance(stream, dict)):
                result["error"] = f"{expected_type} artifact has no {expected_type} stream"
                return result
            duration = float(payload["format"].get("duration", "nan"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result["error"] = "invalid media ffprobe output"
            return result
        if not math.isfinite(duration):
            result["error"] = "media duration is not finite"
            return result
        result["duration_seconds"] = duration
        result["valid"] = duration > 0
        return result
    finally:
        os.close(descriptor)


def _storyboard_info(root: Path) -> dict[str, Any]:
    sb = root / "storyboard.json"
    result: dict[str, Any] = {"expected_scenes": 0, "scene_numbers": [], "errors": [], "valid": False}
    if not _safe_regular_file(root, sb):
        return result
    try:
        data = read_json_artifact(sb)
    except ValueError as exc:
        if "Expected JSON object" in str(exc):
            result["errors"].append("storyboard.json must be an object")
        else:
            result["errors"].append(f"storyboard.json invalid or unreadable: {exc}")
        return result
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
    explicit_numbers = [scene.get("scene_number") for scene in scenes]
    if (
        len(explicit_numbers) != len(scenes)
        or any(isinstance(number, bool) or not isinstance(number, int) or number <= 0 for number in explicit_numbers)
        or len(set(explicit_numbers)) != len(explicit_numbers)
        or set(explicit_numbers) != set(range(1, len(scenes) + 1))
    ):
        result["errors"].append("storyboard.json scene numbers must be contiguous, unique, and positive.")
        return result
    result["valid"] = True
    result["scene_numbers"] = list(range(1, len(scenes) + 1))
    return result


def _generation_plan_info(root: Path) -> dict[str, Any]:
    """Validate the generation plan and expose its scene identity."""
    plan_path = root / "clips" / "generation_plan.json"
    result: dict[str, Any] = {"scene_numbers": [], "scene_hashes": {}, "errors": [], "valid": False, "status": None}
    if not _safe_regular_file(root, plan_path):
        return result
    try:
        plan = read_json_artifact(plan_path)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        result["errors"].append(f"generation_plan.json invalid or unreadable: {exc}")
        return result
    scenes = plan.get("scenes")
    result["status"] = plan.get("status")
    total_scenes = plan.get("total_scenes")
    scene_statuses = [scene.get("status") for scene in scenes if isinstance(scene, dict)] if isinstance(scenes, list) else []
    if (
        plan.get("status") not in {"completed", "dry_run"}
        or not isinstance(scenes, list)
        or not scenes
        or isinstance(total_scenes, bool)
        or not isinstance(total_scenes, int)
        or total_scenes != len(scenes)
        or any(not isinstance(scene, dict) for scene in scenes)
        or any(
            scene.get("target_file") != f"clips/scene_{scene.get('scene_number'):02d}.mp4"
            for scene in scenes
            if isinstance(scene, dict) and isinstance(scene.get("scene_number"), int) and not isinstance(scene.get("scene_number"), bool)
        )
        or any(status not in {"downloaded", "verified", "dry_run"} for status in scene_statuses)
    ):
        result["errors"].append("generation_plan.json has invalid scene coverage.")
        return result
    scene_numbers = [scene.get("scene_number") for scene in scenes]
    if (
        any(isinstance(number, bool) or not isinstance(number, int) or number <= 0 for number in scene_numbers)
        or len(set(scene_numbers)) != len(scene_numbers)
        or set(scene_numbers) != set(range(1, len(scenes) + 1))
    ):
        result["errors"].append("generation_plan.json scene numbers must be contiguous, unique, and positive.")
        return result
    result["scene_numbers"] = scene_numbers
    if plan.get("status") == "completed":
        scene_hashes: dict[int, str] = {}
        for scene in scenes:
            digest = scene.get("sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                result["errors"].append("completed generation plan is missing a valid scene sha256.")
                return result
            scene_hashes[scene["scene_number"]] = digest
        result["scene_hashes"] = scene_hashes
    result["valid"] = True
    return result


def _expected_scene_count(root: Path) -> int:
    return int(_storyboard_info(root)["expected_scenes"])


def _clip_scene_number(name: str) -> int | None:
    """Return a scene number only for the canonical zero-padded filename."""
    match = re.fullmatch(r"scene_([0-9]+)\.mp4", name)
    if not match:
        return None
    number = int(match.group(1))
    return number if name == f"scene_{number:02d}.mp4" else None


def _sha256_regular_file(path: Path) -> str | None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            return None
        digest = hashlib.sha256()
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _valid_clip_files(root: Path, expected_hashes: dict[int, str] | None = None) -> list[dict[str, Any]]:
    clips_dir = root / "clips"
    if clips_dir.is_symlink() or not clips_dir.is_dir():
        return []
    verified = []
    for path in sorted(clips_dir.glob("scene_*.mp4")):
        scene_number = _clip_scene_number(path.name)
        if scene_number is None:
            continue
        if not _safe_regular_file(root, path):
            continue
        info = _probe_media(path)
        if info.get("valid"):
            if expected_hashes is not None and _sha256_regular_file(path) != expected_hashes.get(scene_number):
                continue
            info["scene_number"] = scene_number
            verified.append(info)
    return verified


def _clip_file_coverage(root: Path) -> tuple[set[int], set[int], list[str]]:
    """Inspect every scene clip filename, including invalid artifacts."""
    clips_dir = root / "clips"
    scene_numbers: list[int] = []
    invalid_files: list[str] = []
    if clips_dir.is_symlink() or not clips_dir.is_dir():
        return set(), set(), []
    for path in sorted(clips_dir.iterdir()):
        if path.name == "generation_plan.json":
            continue
        if not _safe_regular_file(root, path):
            invalid_files.append(path.name)
            continue
        scene_number = _clip_scene_number(path.name)
        if scene_number is None:
            invalid_files.append(path.name)
            continue
        scene_numbers.append(scene_number)
    duplicates = {number for number in scene_numbers if scene_numbers.count(number) > 1}
    return set(scene_numbers), duplicates, invalid_files


def _has_visual_files(root: Path) -> bool:
    visuals_fd = -1
    try:
        visuals_fd = _open_directory_no_follow(root / "visuals", create=False)
    except OSError:
        return False
    try:
        for name in os.listdir(visuals_fd):
            if not fnmatch.fnmatch(name, "scene_*.png"):
                continue
            descriptor = -1
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=visuals_fd,
                )
                file_stat = os.fstat(descriptor)
                if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 8:
                    continue
                if os.read(descriptor, 8) == b"\x89PNG\r\n\x1a\n":
                    return True
            except OSError:
                continue
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        return False
    finally:
        os.close(visuals_fd)


def compute_package_status(
    root: str | Path,
    job_status: str | None = None,
    expected_final_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
    expected_job_id: str | None = None,
    expected_attempt: int | None = None,
    expected_run_id: str | None = None,
    expected_source_digest: str | None = None,
) -> dict[str, Any]:
    """Compute package status from actual output artifacts.

    Returns a JSON-serializable summary. The status field is intentionally more
    precise than job.status; `completed` can still mean only an editor package.
    """
    root = Path(root)
    identity_errors: list[str] = []
    provenance: dict[str, Any] = {}
    identity_required = expected_run_id != LEGACY_FLAT_RUN_ID
    if identity_required and any(value is not None for value in (expected_job_id, expected_attempt, expected_run_id, expected_source_digest)):
        try:
            provenance = read_json_artifact(root / "run_provenance.json")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            identity_errors.append(f"run_provenance.json invalid or unreadable: {exc}")
        if not isinstance(provenance, dict):
            identity_errors.append("run_provenance.json must be an object")
            provenance = {}
        if expected_job_id is not None and provenance.get("job_id") != expected_job_id:
            identity_errors.append("run provenance job_id does not match current job")
        if expected_attempt is not None and provenance.get("attempt") != expected_attempt:
            identity_errors.append("run provenance attempt does not match current job")
        if expected_run_id is not None and provenance.get("run_id") != expected_run_id:
            identity_errors.append("run provenance run_id does not match current job")
        if expected_source_digest is not None and provenance.get("source_digest") != expected_source_digest:
            identity_errors.append("run provenance source digest does not match current job")

    storyboard = _storyboard_info(root) if not identity_errors else {"expected_scenes": 0, "scene_numbers": [], "errors": [], "valid": False}
    generation_plan = _generation_plan_info(root) if not identity_errors else {"scene_numbers": [], "scene_hashes": {}, "errors": [], "valid": False, "status": None}
    expected_scenes = int(storyboard["expected_scenes"])
    valid_clips = _valid_clip_files(
        root,
        generation_plan.get("scene_hashes") if generation_plan.get("status") == "completed" else None,
    )
    clip_scene_numbers = {clip["scene_number"] for clip in valid_clips if isinstance(clip.get("scene_number"), int)}
    all_clip_scene_numbers, duplicate_clip_scene_numbers, invalid_clip_files = _clip_file_coverage(root)
    expected_clip_numbers = set(range(1, expected_scenes + 1))
    missing_clip_numbers = sorted(expected_clip_numbers - clip_scene_numbers)
    extra_clip_numbers = sorted(all_clip_scene_numbers - expected_clip_numbers)
    matching_generation_plan = (
        bool(storyboard["valid"])
        and bool(generation_plan["valid"])
        and generation_plan["scene_numbers"] == storyboard["scene_numbers"]
    )
    plan_sha256 = _sha256_regular_file(root / "clips" / "generation_plan.json") if generation_plan["valid"] else None
    plan_identity_matches = (
        isinstance(expected_plan_sha256, str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256))
        and plan_sha256 == expected_plan_sha256
    )
    complete_clip_coverage = (
        bool(expected_scenes)
        and matching_generation_plan
        and generation_plan.get("status") == "completed"
        and not missing_clip_numbers
        and not extra_clip_numbers
        and not duplicate_clip_scene_numbers
        and not invalid_clip_files
        and len(valid_clips) == expected_scenes
    )
    final_path = root / "final" / "video.mp4"
    final_video = _probe_media(final_path) if _safe_regular_file(root, final_path) else {
        "path": str(final_path),
        "exists": False,
        "size_bytes": 0,
        "ffprobe_checked": False,
        "valid": False,
        "error": "final media path contains a symlink or non-regular file",
    }
    observed_final_sha256 = _sha256_regular_file(final_path) if final_video.get("valid", False) else None
    expected_final_digest_valid = (
        isinstance(expected_final_sha256, str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", expected_final_sha256))
    )
    # A worker may already have committed descriptor-bound evidence for the
    # verified attempt.  Keep that immutable value in the manifest; the
    # pathname hash is only an identity check and must never replace the
    # evidence with bytes from a destination that was swapped after publish.
    final_sha256 = (
        expected_final_sha256
        if expected_final_digest_valid and final_video.get("valid", False)
        else observed_final_sha256
    )
    final_identity_matches = (
        expected_final_digest_valid
        and observed_final_sha256 == expected_final_sha256
    )
    final_video_ready = bool(
        not identity_errors
        and
        final_video.get("valid", False)
        and final_identity_matches
        and plan_identity_matches
        and complete_clip_coverage
        and job_status == "completed"
    )

    artifacts = {
        "creative_brief": _has_file(root, "creative_brief.json"),
        "script": _has_file(root, "script.txt"),
        "storyboard": _has_file(root, "storyboard.json") and bool(storyboard["valid"]),
        "visual_prompts": _has_file(root, "visual_prompts.json"),
        "video_prompts": _has_file(root, "video_prompts.json"),
        "voiceover_script": _has_file(root, "audio/voiceover_script.txt"),
        "voiceover_audio": (
            _probe_media(root / "audio" / "voiceover.mp3").get("valid", False)
            if _safe_regular_file(root, root / "audio" / "voiceover.mp3")
            else False
        ),
        "music_prompt": _has_file(root, "music_prompt.txt"),
        "captions": _has_file(root, "captions.srt"),
        "assembly_manifest": _has_file(root, "assembly_manifest.json"),
        "timeline_fcpxml": _has_file(root, "timeline.fcpxml"),
        "generation_plan": bool(generation_plan["valid"]),
        "clips": len(valid_clips),
        "final_video": final_video_ready,
    }

    if not identity_errors and job_status == STATUS_FAILED:
        package_status = STATUS_FAILED
    elif artifacts["final_video"]:
        package_status = STATUS_FINAL_VIDEO_READY
    elif not identity_errors and complete_clip_coverage:
        package_status = STATUS_CLIPS_GENERATED
    elif not identity_errors and all(
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
    elif not identity_errors and artifacts["video_prompts"]:
        package_status = STATUS_PROMPT_PACKAGE_ONLY
    elif not identity_errors and artifacts["script"] and artifacts["storyboard"]:
        package_status = STATUS_SCRIPT_PACKAGE
    elif not identity_errors and artifacts["creative_brief"]:
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
        "artifact_errors": identity_errors + storyboard["errors"] + generation_plan["errors"],
        "final_video_probe": final_video,
        "final_video_sha256": final_sha256,
        "final_video_identity_matches": final_identity_matches,
        "generation_plan_sha256": plan_sha256,
        "generation_plan_identity_matches": plan_identity_matches,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _public_summary(summary: dict[str, Any], root: Path) -> dict[str, Any]:
    """Return manifest-safe package summary without absolute filesystem paths."""
    cleaned = dict(summary)
    probe = cleaned.get("final_video_probe")
    if isinstance(probe, dict):
        probe = dict(probe)
        raw_path = probe.get("path")
        if isinstance(raw_path, str):
            try:
                probe["path"] = str(Path(raw_path).relative_to(root))
            except ValueError:
                probe.pop("path", None)
        cleaned["final_video_probe"] = probe
    return cleaned


def write_package_manifest(root: str | Path, job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write package_manifest.json and return its contents."""
    root = Path(root)
    root_fd = _open_directory_no_follow(root, create=True)
    os.close(root_fd)
    durable_identity = (job or {}).get("run_id")
    summary = compute_package_status(
        root,
        (job or {}).get("status"),
        (job or {}).get("final_video_sha256"),
        (job or {}).get("final_video_plan_sha256"),
        (job or {}).get("id") if durable_identity else None,
        (job or {}).get("attempt") if durable_identity and isinstance((job or {}).get("attempt"), int) else None,
        durable_identity,
        (job or {}).get("source_digest") if durable_identity else None,
    )
    summary = _public_summary(summary, root)
    job_snapshot = {
        key: (job or {})[key]
        for key in (
            "id", "status", "stage", "progress", "created_at", "updated_at",
            "completed_at", "cancelled_at", "error", "topic", "target_audience",
            "key_messages", "visual_style", "call_to_action", "format", "chapters",
            "scenes", "duration_seconds", "platform", "tone", "final_video_sha256",
            "final_video_plan_sha256", "attempt", "run_id", "source_digest",
        )
        if key in (job or {})
    }
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
