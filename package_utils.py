"""Artifact-derived package status helpers for Solo Studio.

These helpers make output honesty explicit: a job is not a final video just
because the pipeline process completed. Status is derived from files that
actually exist in the job output directory.
"""
from __future__ import annotations

import contextlib
import hashlib
import ctypes
import errno
import json
import math
import os
import stat
import re
import shutil
import subprocess
import fcntl
import fnmatch
import selectors
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, cast


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is not allowed: {key}")
        result[key] = value
    return result


def _cleanup_identity(result: os.stat_result) -> tuple[object, ...]:
    """Return a compound identity token that detects common inode reuse."""
    return (result.st_dev, result.st_ino, result.st_ctime_ns, result.st_mode, result.st_size)


def _directory_cleanup_identity(directory_fd: int) -> tuple[object, ...]:
    """Return a descriptor-relative identity including a directory child snapshot."""
    identity = _cleanup_identity(os.fstat(directory_fd))
    children: list[tuple[str, tuple[object, ...]]] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            children.append((entry.name, _cleanup_identity(entry.stat(follow_symlinks=False))))
    return identity + (tuple(sorted(children)),)


def _entry_cleanup_identity_at(parent_fd: int, name: str) -> tuple[object, ...]:
    """Read an entry identity without following symlinks, including directory contents."""
    entry_stat = os.lstat(name, dir_fd=parent_fd)
    if not stat.S_ISDIR(entry_stat.st_mode):
        return _cleanup_identity(entry_stat)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(name, directory_flags, dir_fd=parent_fd)
    try:
        return _directory_cleanup_identity(directory_fd)
    finally:
        os.close(directory_fd)


def _identity_matches(current: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    if expected and expected[-1] in {"held-directory", "held-descriptor"}:
        return len(current) >= 2 and current[:2] == expected[:2]
    return _claim_identity(current) == _claim_identity(expected)


def _preclaim_identity_matches(current: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    """Compare an entry before rename, retaining regular-file ctime."""
    if expected and expected[-1] in {"held-directory", "held-descriptor"}:
        return len(current) >= 2 and current[:2] == expected[:2]
    if len(current) > 5 or len(expected) > 5:
        return _claim_identity(current) == _claim_identity(expected)
    return current == expected


def _claim_identity(identity: tuple[object, ...]) -> tuple[object, ...]:
    """Return identity fields stable across a same-filesystem rename claim."""
    if len(identity) > 5:
        # A directory's own ctime changes whenever its contents change; the
        # child snapshot remains the mutation signal for directory cleanup.
        return (identity[0], identity[1], identity[3], identity[4], identity[5])
    # Linking and renaming a regular file also changes its ctime.
    return identity[:2] + identity[3:5]


MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_SUBPROCESS_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_CLEANUP_CONTENT_BYTES = 256 * 1024 * 1024
MAX_MEDIA_HASH_BYTES = 256 * 1024 * 1024


def _read_bounded_utf8(descriptor: int, *, deadline: float | None = None, label: str = "text") -> str:
    """Read UTF-8 through a descriptor with a hard byte bound and deadline."""
    file_stat = os.fstat(descriptor)
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if file_stat.st_size > MAX_JSON_BYTES:
        raise ValueError(f"{label} exceeds the {MAX_JSON_BYTES}-byte limit")
    chunks: list[bytes] = []
    total = 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"{label} read deadline exceeded")
        chunk = os.read(descriptor, min(1024 * 1024, MAX_JSON_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_JSON_BYTES:
            raise ValueError(f"{label} exceeds the {MAX_JSON_BYTES}-byte limit")
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8") from exc


def _parse_strict_json(content: str) -> Any:
    """Parse untrusted JSON without duplicate keys, NaN/Infinity, or deep nesting."""
    depth = 0
    in_string = False
    escaped = False
    for character in content:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > 256:
                raise ValueError("JSON nesting exceeds the safety limit")
        elif character in "]}":
            depth = max(0, depth - 1)
    return json.loads(
        content,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_json_constant,
    )


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
DEFAULT_OUTPUT_PROFILE = "landscape"
OUTPUT_PROFILES = {
    "landscape": {
        "output_profile": "landscape",
        "aspect_ratio": "16:9",
        "resolution": "1920x1080",
        "width": 1920,
        "height": 1080,
    },
    "vertical": {
        "output_profile": "vertical",
        "aspect_ratio": "9:16",
        "resolution": "1080x1920",
        "width": 1080,
        "height": 1920,
    },
}


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
    "source_manifest.json",
    "source_context.md",
    "reverse_brief.json",
    "visuals",
    "audio",
    "clips",
    "final",
]


def _sanitized_subprocess_env() -> dict[str, str]:
    """Return only non-secret runtime variables for child tools."""
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_CONFIG_HOME"}
    environment = {key: value for key, value in os.environ.items() if key in allowed and value}
    environment.setdefault("PATH", os.defpath)
    environment["NO_COLOR"] = "1"
    return environment


def _set_response_timeout(response: Any, timeout: float) -> None:
    """Apply the remaining deadline to urllib/http response sockets when available."""
    bounded = max(0.001, float(timeout))
    candidates = [
        response,
        getattr(response, "raw", None),
        getattr(response, "fp", None),
        getattr(getattr(response, "raw", None), "_fp", None),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        for attribute in ("_sock", "sock"):
            sock = getattr(candidate, attribute, None)
            if sock is not None and hasattr(sock, "settimeout"):
                sock.settimeout(bounded)
                return


def _fsync_verified_publication(
    directory_fd: int,
    name: str,
    expected_inode: tuple[int, int],
    *,
    deadline: float | None = None,
) -> None:
    """Durably publish and revalidate one expected inode at its canonical name."""
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("publication durability deadline exceeded")
        current = os.fstat(descriptor)
        published = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != expected_inode or (published.st_dev, published.st_ino) != expected_inode:
            raise OSError("publication did not expose the verified inode")
        os.fsync(descriptor)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("publication durability deadline exceeded")
        os.fsync(directory_fd)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("publication durability deadline exceeded")
        current_after_fsync = os.fstat(descriptor)
        published_after_fsync = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (current_after_fsync.st_dev, current_after_fsync.st_ino) != expected_inode or (
            published_after_fsync.st_dev,
            published_after_fsync.st_ino,
        ) != expected_inode:
            raise OSError("publication changed after durability barrier")
    finally:
        os.close(descriptor)


def _acquire_exclusive_lock(directory_fd: int, *, deadline: float | None = None) -> None:
    while True:
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("publication lock deadline exceeded")
            time.sleep(min(0.01, max(0.0, (deadline - time.monotonic()) if deadline is not None else 0.01)))


@contextlib.contextmanager
def _publication_lock(directory_fd: int, target_name: str, *, deadline: float | None = None) -> Iterator[None]:
    """Serialize publishers on the stable output-directory inode."""
    del target_name
    while True:
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("publication lock deadline exceeded")
            time.sleep(0.01)
    try:
        yield
    finally:
        fcntl.flock(directory_fd, fcntl.LOCK_UN)


def _enable_subreaper() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        return prctl(36, 1, 0, 0, 0) == 0
    except (AttributeError, OSError):
        return False


def _process_observation(pid: int) -> tuple[str, tuple[int, int, str] | None]:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = text.rfind(")")
        fields = text[closing + 2 :].split()
        if len(fields) <= 19:
            return "unknown", None
        return "present", (int(fields[1]), int(fields[19]), fields[0])
    except (FileNotFoundError, ProcessLookupError):
        return "gone", None
    except (PermissionError, ValueError, OSError):
        return "unknown", None


def _process_identity(pid: int) -> tuple[int, int, str] | None:
    status, identity = _process_observation(pid)
    return identity if status == "present" else None


def _direct_children_state(parent_pid: int) -> tuple[dict[int, int], bool]:
    children: dict[int, int] = {}
    complete = True
    try:
        entries = Path("/proc").iterdir()
    except (FileNotFoundError, PermissionError, OSError):
        return children, False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        status, identity = _process_observation(pid)
        if status == "unknown":
            complete = False
            continue
        if identity is not None and identity[0] == parent_pid:
            children[pid] = identity[1]
    return children, complete


def _direct_children(parent_pid: int) -> dict[int, int]:
    return _direct_children_state(parent_pid)[0]


def _track_descendants(root_pid: int, tracked: dict[int, int]) -> bool:
    frontier = dict(tracked)
    for depth in range(8):
        changed = False
        try:
            entries = Path("/proc").iterdir()
        except (FileNotFoundError, PermissionError, OSError):
            return False
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid in tracked:
                continue
            identity = _process_identity(pid)
            if identity is None:
                continue
            parent_starttime = frontier.get(identity[0])
            parent_identity = _process_identity(identity[0]) if parent_starttime is not None else None
            if parent_identity is None or parent_identity[1] != parent_starttime:
                continue
            tracked[pid] = identity[1]
            frontier[pid] = identity[1]
            changed = True
        if not changed:
            return True
        if depth == 7:
            return False
    return True

def _track_adopted_descendants(
    supervisor_pid: int,
    baseline_children: dict[int, int],
    tracked: dict[int, int],
) -> bool:
    """Validate adopted children without claiming unrelated post-baseline children."""
    children, complete = _direct_children_state(supervisor_pid)
    boundary_ok = complete
    for pid, starttime in children.items():
        if baseline_children.get(pid) == starttime:
            continue
        if tracked.get(pid) != starttime:
            boundary_ok = False
    for pid in tuple(tracked):
        boundary_ok = _track_descendants(pid, tracked) and boundary_ok
    return boundary_ok


def _process_has_supervision_token(pid: int, token: str) -> bool:
    try:
        with open(f"/proc/{pid}/environ", "rb") as stream:
            environment = stream.read(64 * 1024)
        if len(environment) >= 64 * 1024:
            raise OSError("supervision environment read was truncated")
        needle = b"SOLO_STUDIO_SUPERVISION_TOKEN=" + token.encode("ascii")
        return needle in environment.split(b"\0")
    except FileNotFoundError:
        return False
    except (PermissionError, OSError) as exc:
        raise OSError("supervision token could not be verified") from exc


def _track_owned_processes(root_pid: int, tracked: dict[int, int], token: str) -> bool:
    """Refresh the launched lineage only after verifying its supervision token."""
    try:
        token_owned = _process_has_supervision_token(root_pid, token)
    except OSError:
        return False
    if not token_owned:
        return False
    return _track_descendants(root_pid, tracked)


def _terminate_tracked_processes(
    process: subprocess.Popen[Any],
    baseline_children: dict[int, int],
    tracked: dict[int, int] | None = None,
    supervision_token: str | None = None,
    *,
    deadline: float | None = None,
) -> bool:
    if tracked is None:
        tracked = {}
        root_identity = _process_identity(process.pid)
        if root_identity is not None:
            tracked[process.pid] = root_identity[1]
    supervision_ok = True
    token = supervision_token
    def refresh_supervision() -> bool:
        boundary_ok = _track_adopted_descendants(os.getpid(), baseline_children, tracked)
        descendants_ok = _track_descendants(process.pid, tracked)
        if _process_identity(process.pid) is None:
            return boundary_ok and descendants_ok
        assert token is not None
        return boundary_ok and descendants_ok and _track_owned_processes(process.pid, tracked, token)

    if supervision_token is not None:
        supervision_ok = refresh_supervision() and supervision_ok
    else:
        supervision_ok = _track_adopted_descendants(os.getpid(), baseline_children, tracked) and supervision_ok
        supervision_ok = _track_descendants(process.pid, tracked) and supervision_ok
    root_status, root_current = _process_observation(process.pid)
    root_identity_ok = (root_status == "gone") or (
        root_current is not None and tracked.get(process.pid) == root_current[1]
    )
    if root_current is not None and tracked.get(process.pid) == root_current[1]:
        _terminate_process_group(process)
    for _ in range(8):
        expired = deadline is not None and time.monotonic() >= deadline
        if expired:
            supervision_ok = False
        if supervision_token is not None:
            supervision_ok = refresh_supervision() and supervision_ok
        else:
            supervision_ok = _track_adopted_descendants(os.getpid(), baseline_children, tracked) and supervision_ok
            supervision_ok = _track_descendants(process.pid, tracked) and supervision_ok
        for pid, starttime in list(tracked.items()):
            current_status, current = _process_observation(pid)
            if current_status == "unknown":
                supervision_ok = False
                continue
            if current is None or current[1] != starttime:
                continue
            if current[2] == "Z":
                if pid != process.pid:
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except (ChildProcessError, OSError):
                        pass
                continue
            if pid != process.pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        if expired:
            time.sleep(0.01)
            break
    for pid, starttime in list(tracked.items()):
        if pid == process.pid:
            continue
        current_status, current = _process_observation(pid)
        if current_status == "unknown":
            supervision_ok = False
            continue
        if current is not None and current[1] == starttime and current[2] == "Z":
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
    try:
        process.wait(timeout=0)
    except subprocess.TimeoutExpired:
        pass
    final_root_status, final_root = _process_observation(process.pid)
    if final_root_status == "unknown":
        root_identity_ok = False
    elif final_root_status == "present":
        root_identity_ok = root_identity_ok and final_root is not None and final_root[1] == tracked.get(process.pid)
    return supervision_ok and root_identity_ok and process.poll() is not None and not any(
        (current := _process_identity(pid)) is not None and current[1] == starttime and current[2] != "Z"
        for pid, starttime in tracked.items()
        if pid != process.pid
    )


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    """Stop the child and every descendant in its private process group."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass


def _run_bounded_subprocess(
    command: list[str],
    *,
    timeout: float,
    pass_fds: tuple[int, ...] = (),
    max_output_bytes: int = 256 * 1024,
    deadline: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a child with bounded stdout/stderr and a hard wall-clock deadline."""
    try:
        timeout_value = float(timeout)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("subprocess limits are invalid")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout_value)
        or not 0 < timeout_value <= MAX_SUBPROCESS_TIMEOUT_SECONDS
        or isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes <= 0
    ):
        raise ValueError("subprocess limits are invalid")
    launch_deadline = time.monotonic() + timeout_value
    if deadline is not None:
        if deadline <= time.monotonic():
            raise subprocess.TimeoutExpired(command, timeout)
        launch_deadline = min(launch_deadline, deadline)
    subreaper_enabled = _enable_subreaper()
    if sys.platform.startswith("linux") and not subreaper_enabled:
        raise OSError("subprocess descendant supervision unavailable")
    baseline_children, baseline_complete = _direct_children_state(os.getpid()) if subreaper_enabled else ({}, True)
    if not baseline_complete:
        raise OSError("subprocess child enumeration unavailable")
    supervision_token = uuid.uuid4().hex
    child_environment = _sanitized_subprocess_env()
    child_environment["SOLO_STUDIO_SUPERVISION_TOKEN"] = supervision_token
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_environment,
        pass_fds=pass_fds,
        close_fds=True,
        start_new_session=True,
    )
    tracked_processes: dict[int, int] = {}
    root_identity = _process_identity(process.pid)
    if root_identity is not None:
        tracked_processes[process.pid] = root_identity[1]
    selector = selectors.DefaultSelector()
    streams = [stream for stream in (process.stdout, process.stderr) if stream is not None]
    stream_fds = {id(stream): stream.fileno() for stream in streams}
    buffers = {fd: bytearray() for fd in stream_fds.values()}
    for stream in streams:
        selector.register(stream, selectors.EVENT_READ)
    stdout_fd = stream_fds.get(id(process.stdout)) if process.stdout is not None else None
    stderr_fd = stream_fds.get(id(process.stderr)) if process.stderr is not None else None
    deadline = launch_deadline
    forced_reason: str | None = None
    try:
        while selector.get_map() or process.poll() is None:
            if subreaper_enabled:
                _track_descendants(process.pid, tracked_processes)
            now = time.monotonic()
            if forced_reason is None:
                remaining = deadline - now
                if remaining <= 0:
                    forced_reason = "timeout"
                    if subreaper_enabled:
                        _terminate_tracked_processes(
                            process, baseline_children, tracked_processes, supervision_token, deadline=deadline
                        )
                    else:
                        _terminate_process_group(process)
                    break
            else:
                remaining = 0.0
            events = selector.select(min(0.1, remaining))
            if not events:
                continue
            for key, _ in events:
                stream = key.fileobj
                fd = key.fd
                try:
                    chunk = os.read(fd, 64 * 1024)
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(stream)
                    close_method = getattr(stream, "close", None)
                    if callable(close_method):
                        close_method()
                    continue
                buffer = buffers[fd]
                remaining_capacity = max_output_bytes - len(buffer)
                if remaining_capacity > 0:
                    buffer.extend(chunk[:remaining_capacity])
                if len(chunk) > remaining_capacity and forced_reason is None:
                    forced_reason = "output_limit"
                    if subreaper_enabled:
                        _terminate_tracked_processes(
                            process, baseline_children, tracked_processes, supervision_token, deadline=deadline
                        )
                    else:
                        _terminate_process_group(process)
                    break
            if forced_reason is not None:
                break
        if forced_reason is not None:
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                if subreaper_enabled:
                    _terminate_tracked_processes(
                        process, baseline_children, tracked_processes, supervision_token, deadline=deadline
                    )
                else:
                    _terminate_process_group(process)
            raise subprocess.TimeoutExpired(command, timeout)
        else:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if subreaper_enabled and not _terminate_tracked_processes(
                process, baseline_children, tracked_processes, supervision_token, deadline=deadline
            ):
                forced_reason = "descendant"
    finally:
        selector.close()
        for stream in streams:
            close_method = getattr(stream, "close", None)
            if callable(close_method):
                close_method()
    stdout = bytes(buffers.get(stdout_fd, bytearray()) if stdout_fd is not None else b"").decode("utf-8", "replace")
    stderr = bytes(buffers.get(stderr_fd, bytearray()) if stderr_fd is not None else b"").decode("utf-8", "replace")
    if forced_reason == "descendant":
        raise subprocess.SubprocessError("subprocess descendant supervision failed")
    if forced_reason == "timeout":
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
    if forced_reason == "output_limit":
        stderr = f"{stderr}\n[child output exceeded {max_output_bytes} bytes]"
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def select_output_profile_input(payload: Mapping[str, Any]) -> Any:
    """Select explicit profile/aspect input without hiding malformed falsy values."""
    if "output_profile" in payload:
        return payload["output_profile"]
    if "aspect_ratio" in payload:
        return payload["aspect_ratio"]
    return None


def normalize_output_profile(value: Any = None) -> dict[str, Any]:
    """Return the supported output profile contract, defaulting old jobs safely."""
    if value is None:
        profile = DEFAULT_OUTPUT_PROFILE
    elif not isinstance(value, str):
        raise ValueError("output_profile must be 'landscape' or 'vertical'")
    else:
        profile = value.strip().lower().replace("-", "_")
        if not profile:
            raise ValueError("output_profile must be 'landscape' or 'vertical'")
    aliases = {
        "16:9": "landscape",
        "landscape_16_9": "landscape",
        "horizontal": "landscape",
        "youtube": "landscape",
        "linkedin": "landscape",
        "9:16": "vertical",
        "vertical_9_16": "vertical",
        "portrait": "vertical",
        "tiktok": "vertical",
        "shorts": "vertical",
        "reels": "vertical",
    }
    profile = aliases.get(profile, profile)
    if profile not in OUTPUT_PROFILES:
        raise ValueError("output_profile must be 'landscape' or 'vertical'")
    return dict(OUTPUT_PROFILES[profile])


def validate_output_profile_contract(payload: Mapping[str, Any], label: str = "output") -> dict[str, Any]:
    """Validate every supplied profile field without hiding malformed metadata."""
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} output profile metadata must be an object")
    metadata_keys = ("output_profile", "aspect_ratio", "resolution")
    if not any(key in payload for key in metadata_keys):
        return normalize_output_profile(None)
    if "output_profile" in payload or "aspect_ratio" in payload:
        profile = normalize_output_profile(select_output_profile_input(payload))
    else:
        resolution = payload["resolution"]
        if not isinstance(resolution, str) or not resolution.strip():
            raise ValueError(f"{label} output profile metadata is invalid")
        matches = [candidate for candidate in OUTPUT_PROFILES.values() if candidate["resolution"] == resolution.strip()]
        if len(matches) != 1:
            raise ValueError(f"{label} output profile metadata is invalid")
        profile = dict(matches[0])
    for key in metadata_keys:
        if key in payload and payload[key] != profile[key]:
            raise ValueError(f"{label} output profile metadata disagrees")
    return dict(profile)


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
        if job.get("status") not in {"completed", "editor_package", "failed", "cancelled"}:
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


CLEANUP_HASH_MAX_SECONDS = 5.0
PUBLICATION_CLEANUP_GRACE_SECONDS = 1.0


def _regular_content_digest_at(
    parent_fd: int,
    name: str,
    *,
    deadline: float | None = None,
) -> str:
    deadline = time.monotonic() + CLEANUP_HASH_MAX_SECONDS if deadline is None else deadline
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        dir_fd=parent_fd,
    )
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_CLEANUP_CONTENT_BYTES:
            raise OSError("cleanup content exceeds the configured limit")

        def hash_once() -> str:
            digest = hashlib.sha256()
            total = 0
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("cleanup content hashing deadline exceeded")
                chunk = os.read(descriptor, min(1024 * 1024, MAX_CLEANUP_CONTENT_BYTES + 1 - total))
                if not chunk:
                    final_stat = os.fstat(descriptor)
                    if (final_stat.st_dev, final_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino) or final_stat.st_size != file_stat.st_size:
                        raise OSError("cleanup content changed during hashing")
                    return digest.hexdigest()
                total += len(chunk)
                if total > MAX_CLEANUP_CONTENT_BYTES:
                    raise OSError("cleanup content exceeds the configured limit")
                digest.update(chunk)

        first_digest = hash_once()
        second_digest = hash_once()
        if first_digest != second_digest:
            raise OSError("cleanup content changed between hashing passes")
        return second_digest
    finally:
        os.close(descriptor)


def _regular_content_digest_descriptor(
    descriptor: int,
    *,
    deadline: float | None = None,
) -> str:
    """Hash a held regular-file descriptor with mutation-sensitive double reads."""
    deadline = time.monotonic() + CLEANUP_HASH_MAX_SECONDS if deadline is None else deadline
    file_stat = os.fstat(descriptor)
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_CLEANUP_CONTENT_BYTES:
        raise OSError("cleanup content exceeds the configured limit")

    def hash_once() -> str:
        digest = hashlib.sha256()
        total = 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("cleanup content hashing deadline exceeded")
            chunk = os.read(descriptor, min(1024 * 1024, MAX_CLEANUP_CONTENT_BYTES + 1 - total))
            if not chunk:
                final_stat = os.fstat(descriptor)
                if (final_stat.st_dev, final_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino) or final_stat.st_size != file_stat.st_size:
                    raise OSError("cleanup content changed during hashing")
                return digest.hexdigest()
            total += len(chunk)
            if total > MAX_CLEANUP_CONTENT_BYTES:
                raise OSError("cleanup content exceeds the configured limit")
            digest.update(chunk)

    first_digest = hash_once()
    second_digest = hash_once()
    if first_digest != second_digest:
        raise OSError("cleanup content changed between hashing passes")
    return second_digest


def _remove_entry_at(
    parent_fd: int,
    name: str,
    expected_inode: tuple[object, ...] | None = None,
    *,
    deadline: float | None = None,
    held_fd: int | None = None,
    verify_preclaim_ctime: bool = True,
) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("cleanup deadline exceeded")
    if held_fd is not None:
        held_stat = os.fstat(held_fd)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        current_fd = os.open(name, directory_flags, dir_fd=parent_fd)
        try:
            current_stat = os.fstat(current_fd)
            if (current_stat.st_dev, current_stat.st_ino) != (held_stat.st_dev, held_stat.st_ino):
                raise OSError("cleanup entry no longer names the held directory")
        finally:
            os.close(current_fd)
    staging_name, staging_fd = _open_private_staging_directory(parent_fd)
    staging_stat = os.fstat(staging_fd)
    staging_identity = (staging_stat.st_dev, staging_stat.st_ino, "held-directory")
    claimed = False
    removed = False
    entry_fd = -1
    cleanup_guard_name: str | None = None
    expected_content_digest: str | None = None
    try:
        if expected_inode is not None:
            current_identity = _entry_cleanup_identity_at(parent_fd, name)
            identity_matches = (
                _preclaim_identity_matches(current_identity, expected_inode)
                if verify_preclaim_ctime
                else _identity_matches(current_identity, expected_inode)
            )
            if not identity_matches:
                raise OSError("cleanup entry was replaced before it could be claimed")
            current_stat = os.lstat(name, dir_fd=parent_fd)
            if stat.S_ISREG(current_stat.st_mode):
                expected_content_digest = _regular_content_digest_at(parent_fd, name, deadline=deadline)
        os.rename(name, "entry", src_dir_fd=parent_fd, dst_dir_fd=staging_fd)
        claimed = True
        if expected_inode is not None:
            claimed_identity = _entry_cleanup_identity_at(staging_fd, "entry")
            if not _identity_matches(claimed_identity, expected_inode):
                recovery_name = f".recovered-{os.getpid()}-{uuid.uuid4().hex}"
                _rename_noreplace(staging_fd, "entry", parent_fd, recovery_name)
                os.fsync(parent_fd)
                claimed = False
                raise OSError("cleanup entry was replaced while it was being claimed")
            if expected_content_digest is not None:
                claimed_content_digest = _regular_content_digest_at(staging_fd, "entry", deadline=deadline)
                if claimed_content_digest != expected_content_digest:
                    recovery_name = f".recovered-{os.getpid()}-{uuid.uuid4().hex}"
                    _rename_noreplace(staging_fd, "entry", parent_fd, recovery_name)
                    os.fsync(parent_fd)
                    claimed = False
                    raise OSError("cleanup entry content changed while it was being claimed")
                final_claimed_identity = _entry_cleanup_identity_at(staging_fd, "entry")
                if not _preclaim_identity_matches(final_claimed_identity, claimed_identity):
                    recovery_name = f".recovered-{os.getpid()}-{uuid.uuid4().hex}"
                    _rename_noreplace(staging_fd, "entry", parent_fd, recovery_name)
                    os.fsync(parent_fd)
                    claimed = False
                    raise OSError("cleanup entry identity changed after content verification")
                final_content_digest = _regular_content_digest_at(staging_fd, "entry", deadline=deadline)
                if final_content_digest != expected_content_digest:
                    recovery_name = f".recovered-{os.getpid()}-{uuid.uuid4().hex}"
                    _rename_noreplace(staging_fd, "entry", parent_fd, recovery_name)
                    os.fsync(parent_fd)
                    claimed = False
                    raise OSError("cleanup entry content changed after final identity verification")
                post_digest_identity = _entry_cleanup_identity_at(staging_fd, "entry")
                if not _preclaim_identity_matches(post_digest_identity, final_claimed_identity):
                    recovery_name = f".recovered-{os.getpid()}-{uuid.uuid4().hex}"
                    _rename_noreplace(staging_fd, "entry", parent_fd, recovery_name)
                    os.fsync(parent_fd)
                    claimed = False
                    raise OSError("cleanup entry identity changed after final content verification")
        entry_stat = os.lstat("entry", dir_fd=staging_fd)
        if stat.S_ISREG(entry_stat.st_mode) and expected_content_digest is not None:
            entry_fd = os.open(
                "entry",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=staging_fd,
            )
            held_entry_stat = os.fstat(entry_fd)
            if (held_entry_stat.st_dev, held_entry_stat.st_ino) != (entry_stat.st_dev, entry_stat.st_ino):
                raise OSError("cleanup entry changed before destructive action")
            cleanup_guard_name = f".cleanup-guard-{os.getpid()}-{uuid.uuid4().hex}"
            os.link(
                "entry",
                cleanup_guard_name,
                src_dir_fd=staging_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.fsync(parent_fd)
        if stat.S_ISDIR(entry_stat.st_mode):
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            directory_fd = os.open("entry", directory_flags, dir_fd=staging_fd)
            try:
                _remove_tree_contents_at(directory_fd, deadline=deadline)
            finally:
                os.close(directory_fd)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("cleanup deadline exceeded")
            os.rmdir("entry", dir_fd=staging_fd)
        elif stat.S_ISREG(entry_stat.st_mode) or stat.S_ISLNK(entry_stat.st_mode):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("cleanup deadline exceeded")
            os.unlink("entry", dir_fd=staging_fd)
            removed = True
            if entry_fd >= 0:
                post_remove_digest = _regular_content_digest_descriptor(entry_fd, deadline=deadline)
                if post_remove_digest != expected_content_digest:
                    assert cleanup_guard_name is not None
                    recovery_name = f".recovered-{os.getpid()}-{uuid.uuid4().hex}"
                    guard_name = cleanup_guard_name
                    cleanup_guard_name = None
                    try:
                        _rename_noreplace(parent_fd, guard_name, parent_fd, recovery_name)
                    except OSError as recovery_exc:
                        raise OSError("cleanup entry changed and recovery was not proven") from recovery_exc
                    os.fsync(parent_fd)
                    raise OSError("cleanup entry content changed during destructive action")
                if cleanup_guard_name is not None:
                    os.unlink(cleanup_guard_name, dir_fd=parent_fd)
                    cleanup_guard_name = None
                    os.fsync(parent_fd)
        else:
            raise OSError("refusing to remove a non-regular cleanup entry")
        removed = True
    finally:
        if cleanup_guard_name is not None:
            os.unlink(cleanup_guard_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            cleanup_guard_name = None
        if entry_fd >= 0:
            os.close(entry_fd)
        if claimed and not removed:
            try:
                entry_stat = os.lstat("entry", dir_fd=staging_fd)
                if stat.S_ISDIR(entry_stat.st_mode):
                    try:
                        _rename_noreplace(staging_fd, "entry", parent_fd, name)
                    except FileExistsError:
                        recovery_name = f".recovered-{os.getpid()}-{uuid.uuid4().hex}"
                        _rename_noreplace(staging_fd, "entry", parent_fd, recovery_name)
                else:
                    try:
                        os.link("entry", name, src_dir_fd=staging_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                    except FileExistsError:
                        recovery_name = f".recovered-{os.getpid()}-{uuid.uuid4().hex}"
                        _rename_noreplace(staging_fd, "entry", parent_fd, recovery_name)
                    else:
                        os.unlink("entry", dir_fd=staging_fd)
            finally:
                os.close(staging_fd)
        else:
            os.close(staging_fd)
        try:
            current_staging = os.lstat(staging_name, dir_fd=parent_fd)
            if (current_staging.st_dev, current_staging.st_ino) != staging_identity[:2]:
                raise OSError("private staging directory was replaced during cleanup")
            os.rmdir(staging_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _remove_tree_contents_at(directory_fd: int, *, deadline: float | None = None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("cleanup deadline exceeded")
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("cleanup deadline exceeded")
            try:
                entry_identity = _entry_cleanup_identity_at(directory_fd, entry.name)
                _remove_entry_at(directory_fd, entry.name, entry_identity, deadline=deadline)
            except FileNotFoundError:
                pass


def _remove_tree_at(
    parent_fd: int,
    name: str,
    expected_inode: tuple[object, ...] | None = None,
    *,
    deadline: float | None = None,
) -> None:
    """Claim the directory entry before recursively removing its contents."""
    _remove_entry_at(parent_fd, name, expected_inode, deadline=deadline)


def _contain_entry_at(
    parent_fd: int,
    name: str,
    expected_inode: tuple[object, ...],
    quarantine_prefix: str,
    *,
    deadline: float | None = None,
) -> None:
    """Remove the expected entry or atomically quarantine whatever replaced it."""
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("containment deadline exceeded")
    try:
        current = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("containment deadline exceeded")
    if stat.S_ISDIR(current.st_mode):
        directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            actual = _directory_cleanup_identity(directory_fd)
        finally:
            os.close(directory_fd)
    else:
        actual = _cleanup_identity(current)
    if actual == expected_inode:
        try:
            _remove_entry_at(parent_fd, name, expected_inode, deadline=deadline)
            return
        except OSError:
            pass

    quarantine_name = f".{quarantine_prefix}.untrusted-{os.getpid()}-{uuid.uuid4().hex}"
    placeholder_stat = None
    try:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("containment deadline exceeded")
        if stat.S_ISDIR(current.st_mode):
            os.mkdir(quarantine_name, 0o700, dir_fd=parent_fd)
        else:
            placeholder_fd = os.open(
                quarantine_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            os.close(placeholder_fd)
        placeholder_stat = os.lstat(quarantine_name, dir_fd=parent_fd)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("containment deadline exceeded")
        try:
            _rename_exchange(parent_fd, name, parent_fd, quarantine_name)
        except OSError as exchange_error:
            try:
                if stat.S_ISDIR(placeholder_stat.st_mode):
                    os.rmdir(quarantine_name, dir_fd=parent_fd)
                else:
                    os.unlink(quarantine_name, dir_fd=parent_fd)
            except OSError as cleanup_error:
                raise OSError("could not prepare containment quarantine") from cleanup_error
            raise OSError("could not contain replaced entry without atomic quarantine") from exchange_error
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("containment deadline exceeded")
        os.fsync(parent_fd)
        placeholder_identity = _entry_cleanup_identity_at(parent_fd, name)
        _remove_entry_at(parent_fd, name, placeholder_identity, deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("containment deadline exceeded")
        os.fsync(parent_fd)
    except Exception:
        try:
            if placeholder_stat is not None and os.lstat(quarantine_name, dir_fd=parent_fd) == placeholder_stat:
                if stat.S_ISDIR(placeholder_stat.st_mode):
                    os.rmdir(quarantine_name, dir_fd=parent_fd)
                else:
                    os.unlink(quarantine_name, dir_fd=parent_fd)
        except OSError:
            pass
        raise


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
                    entry_stat = os.lstat(name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                else:
                    entry_identity = _entry_cleanup_identity_at(parent_fd, name)
                    _remove_tree_at(parent_fd, name, entry_identity)
                    removed.append(relative)
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
                    _remove_entry_at(root_fd, name, _cleanup_identity(entry_stat))
                    removed.append(name)
                else:
                    raise ValueError(f"refusing to silently skip matching non-file entry: {name}")
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
            child = -1
            try:
                named_before_open = os.lstat(component, dir_fd=descriptor)
                if stat.S_ISLNK(named_before_open.st_mode) or not stat.S_ISDIR(named_before_open.st_mode):
                    raise OSError("directory component is not a regular directory")
                child = -1
                child = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                opened_stat = os.fstat(child)
                named_after_open = os.lstat(component, dir_fd=descriptor)
                if (
                    (named_before_open.st_dev, named_before_open.st_ino) != (opened_stat.st_dev, opened_stat.st_ino)
                    or (named_after_open.st_dev, named_after_open.st_ino) != (opened_stat.st_dev, opened_stat.st_ino)
                ):
                    raise OSError("directory component was replaced before it could be pinned")
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o770, dir_fd=descriptor)
                created_stat = os.lstat(component, dir_fd=descriptor)
                if stat.S_ISLNK(created_stat.st_mode) or not stat.S_ISDIR(created_stat.st_mode):
                    raise OSError("created path is not a directory")
                child = -1
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=descriptor,
                    )
                    opened_stat = os.fstat(child)
                    if (
                        (created_stat.st_dev, created_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino)
                    ):
                        raise OSError("created directory was replaced before it could be pinned")
                except Exception:
                    if child >= 0:
                        os.close(child)
                        child = -1
                    raise
            except Exception:
                if child >= 0:
                    os.close(child)
                    child = -1
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _quarantine_owned_directory_after_observation_failure(
    parent_fd: int,
    name: str,
    descriptor: int,
    expected_inode: tuple[int, int],
) -> None:
    """Remove a newly-created directory when pathname observation failed.

    The directory is exchanged into a private quarantine name before removal;
    the exchanged inode is checked against the held descriptor so a replaced
    pathname is never removed.
    """
    quarantine_name = f".quarantine-{os.getpid()}-{uuid.uuid4().hex}"
    os.mkdir(quarantine_name, 0o700, dir_fd=parent_fd)
    exchanged = False
    try:
        _rename_exchange(parent_fd, name, parent_fd, quarantine_name)
        exchanged = True
        quarantined_stat = os.stat(quarantine_name, dir_fd=parent_fd, follow_symlinks=False)
        if (quarantined_stat.st_dev, quarantined_stat.st_ino) != expected_inode:
            raise OSError("staging directory changed during observation recovery")
        os.rmdir(quarantine_name, dir_fd=parent_fd)
        exchanged = False
        os.rmdir(name, dir_fd=parent_fd)
    except Exception:
        if exchanged:
            try:
                _rename_exchange(parent_fd, name, parent_fd, quarantine_name)
            except OSError:
                raise OSError("could not restore staging directory after observation failure")
        try:
            os.rmdir(quarantine_name, dir_fd=parent_fd)
        except OSError:
            pass
        raise


def _open_private_staging_directory(
    parent_fd: int,
    prefix: str = ".staging-",
    *,
    include_identity: bool = False,
) -> tuple[str, int] | tuple[str, int, tuple[int, int]]:
    """Create a private staging directory and return its name and held FD."""
    if not isinstance(prefix, str) or not prefix or "/" in prefix or "\\" in prefix:
        raise ValueError("staging prefix is invalid")
    for _ in range(5):
        name = f"{prefix}{os.getpid()}-{uuid.uuid4().hex}"
        created_inode: tuple[int, int] | None = None
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            opened_stat = os.fstat(descriptor)
            created_inode = (opened_stat.st_dev, opened_stat.st_ino)
            named_before_open = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (opened_stat.st_dev, opened_stat.st_ino) != (named_before_open.st_dev, named_before_open.st_ino):
                raise OSError("private staging directory was replaced before it was pinned")
            named_stat = os.lstat(name, dir_fd=parent_fd)
            if (opened_stat.st_dev, opened_stat.st_ino) != (named_stat.st_dev, named_stat.st_ino):
                raise OSError("private staging directory was replaced during creation")
            if stat.S_IMODE(opened_stat.st_mode) & 0o777 != 0o700:
                raise OSError("private staging directory permissions changed")
            if include_identity:
                return name, descriptor, created_inode
            return name, descriptor
        except Exception:
            if created_inode is None:
                try:
                    fallback_stat = os.lstat(name, dir_fd=parent_fd)
                    if stat.S_ISDIR(fallback_stat.st_mode):
                        created_inode = (fallback_stat.st_dev, fallback_stat.st_ino)
                except OSError:
                    pass
            if descriptor >= 0:
                if created_inode is not None:
                    try:
                        _quarantine_owned_directory_after_observation_failure(
                            parent_fd, name, descriptor, created_inode
                        )
                    except OSError:
                        pass
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                descriptor = -1
            elif created_inode is not None:
                try:
                    _quarantine_owned_directory_after_observation_failure(
                        parent_fd, name, descriptor, created_inode
                    )
                except OSError:
                    pass
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise
    raise OSError("could not create a unique private staging directory")


def _rename_noreplace(src_fd: int, src_name: str, dst_fd: int, dst_name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOTSUP, "renameat2 is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(src_fd, src_name.encode(), dst_fd, dst_name.encode(), 1)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _rename_exchange(src_fd: int, src_name: str, dst_fd: int, dst_name: str) -> None:
    """Atomically exchange two directory entries using Linux renameat2."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOTSUP, "renameat2 is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(src_fd, src_name.encode(), dst_fd, dst_name.encode(), 2)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _remove_published_mismatch(
    directory_fd: int,
    name: str,
    expected_inode: tuple[object, ...],
    *,
    deadline: float | None = None,
) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("atomic publication rollback deadline exceeded")
    try:
        current = os.lstat(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    if not _identity_matches(_cleanup_identity(current), expected_inode):
        _contain_entry_at(directory_fd, name, expected_inode, "atomic-publication", deadline=deadline)
        raise OSError("published mismatch was replaced; quarantined the replacement")
    if not stat.S_ISREG(current.st_mode):
        raise OSError("published mismatch is not a regular file")
    _remove_entry_at(directory_fd, name, expected_inode, deadline=deadline, verify_preclaim_ctime=False)


def _rollback_atomic_publication(
    directory_fd: int,
    name: str,
    staging_fd: int,
    temporary_name: str,
    backup_fd: int,
    backup_name: str,
    backup_inode: tuple[object, ...] | None,
    backup_digest: str | None,
    displaced_inode: tuple[object, ...] | None,
    published_inode: tuple[object, ...] | None,
    *,
    deadline: float | None = None,
) -> bool:
    """Restore the pinned prior entry after a post-replace publication failure."""
    cleanup_deadline = deadline
    if deadline is not None:
        cleanup_deadline = max(deadline, time.monotonic() + PUBLICATION_CLEANUP_GRACE_SECONDS)
    exchanged_back = False
    if published_inode is not None and displaced_inode is not None and backup_inode is not None and temporary_name:
        try:
            current_stat = os.lstat(name, dir_fd=directory_fd)
            staged_stat = os.lstat(temporary_name, dir_fd=staging_fd)
        except FileNotFoundError:
            current_stat = staged_stat = None
        if (
            current_stat is not None
            and staged_stat is not None
            and _identity_matches(_cleanup_identity(current_stat), published_inode)
            and _identity_matches(_cleanup_identity(staged_stat), displaced_inode)
        ):
            _rename_exchange(staging_fd, temporary_name, directory_fd, name)
            restored_stat = os.lstat(name, dir_fd=directory_fd)
            displaced_stat = os.lstat(temporary_name, dir_fd=staging_fd)
            if not _identity_matches(_cleanup_identity(restored_stat), displaced_inode):
                raise OSError("atomic publication restored the wrong displaced inode")
            if not _identity_matches(_cleanup_identity(displaced_stat), published_inode):
                raise OSError("atomic publication rollback displaced the wrong inode")
            if backup_digest is None:
                raise OSError("atomic publication backup digest was not retained")
            if _regular_content_digest_at(directory_fd, name, deadline=cleanup_deadline) != backup_digest:
                raise OSError("atomic publication restored unexpected backup content")
            exchanged_back = True
    if published_inode is not None and not exchanged_back:
        _remove_published_mismatch(directory_fd, name, published_inode, deadline=cleanup_deadline)
    if backup_inode is not None and not exchanged_back:
        if backup_fd < 0:
            raise OSError("atomic publication backup descriptor was not retained")
        if backup_digest is None:
            raise OSError("atomic publication backup digest was not retained")
        if _regular_content_digest_descriptor(backup_fd, deadline=cleanup_deadline) != backup_digest:
            raise OSError("atomic publication backup content changed")
        os.link(f"/proc/self/fd/{backup_fd}", name, dst_dir_fd=directory_fd, follow_symlinks=True)
        restored_stat = os.lstat(name, dir_fd=directory_fd)
        if not _identity_matches(_cleanup_identity(restored_stat), backup_inode):
            raise OSError("atomic publication restored the wrong backup inode")
        if backup_name:
            _remove_entry_at(staging_fd, backup_name, backup_inode, deadline=cleanup_deadline, verify_preclaim_ctime=False)
    if cleanup_deadline is not None and time.monotonic() >= cleanup_deadline:
        raise TimeoutError("atomic publication rollback deadline exceeded")
    os.fsync(directory_fd)
    return exchanged_back


def _rollback_preserving_primary(
    primary: BaseException,
    directory_fd: int,
    name: str,
    staging_fd: int,
    temporary_name: str,
    backup_fd: int,
    backup_name: str,
    backup_inode: tuple[object, ...] | None,
    backup_digest: str | None,
    displaced_inode: tuple[object, ...] | None,
    published_inode: tuple[object, ...] | None,
    *,
    deadline: float | None = None,
) -> bool:
    try:
        return _rollback_atomic_publication(
            directory_fd, name, staging_fd, temporary_name, backup_fd, backup_name,
            backup_inode, backup_digest, displaced_inode, published_inode, deadline=deadline,
        )
    except BaseException as cleanup:
        contain_deadline = deadline
        if deadline is not None:
            contain_deadline = max(deadline, time.monotonic() + PUBLICATION_CLEANUP_GRACE_SECONDS)
        if published_inode is not None:
            try:
                _contain_entry_at(
                    directory_fd,
                    name,
                    published_inode,
                    "atomic-publication-failure",
                    deadline=contain_deadline,
                )
            except (FileNotFoundError, OSError, TimeoutError):
                pass
        raise primary.with_traceback(primary.__traceback__) from cleanup


def _exchange_existing_publication(
    directory_fd: int,
    name: str,
    staging_fd: int,
    temporary_name: str,
    temporary_inode: tuple[object, ...],
    expected_inode: tuple[object, ...],
    backup_fd: int,
    backup_name: str,
    backup_inode: tuple[object, ...] | None,
    backup_digest: str | None,
    *,
    deadline: float | None = None,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    current = os.lstat(name, dir_fd=directory_fd)
    if not _identity_matches(_cleanup_identity(current), expected_inode):
        raise OSError("destination changed before atomic exchange")
    _rename_exchange(staging_fd, temporary_name, directory_fd, name)
    published_inode = temporary_inode
    try:
        displaced = _cleanup_identity(os.lstat(temporary_name, dir_fd=staging_fd))
    except Exception as primary:
        _rollback_preserving_primary(
            primary, directory_fd, name, staging_fd, temporary_name, backup_fd, backup_name,
            backup_inode, backup_digest, expected_inode, published_inode, deadline=deadline,
        )
        raise
    if not _identity_matches(displaced, expected_inode):
        quarantine_name = f".atomic-publication-untrusted-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            _rename_noreplace(staging_fd, temporary_name, directory_fd, quarantine_name)
        except BaseException as primary:
            _rollback_preserving_primary(
                primary, directory_fd, name, staging_fd, temporary_name, backup_fd, backup_name,
                backup_inode, backup_digest, expected_inode, published_inode, deadline=deadline,
            )
            raise
        _rollback_preserving_primary(
            OSError("destination changed during atomic exchange"),
            directory_fd, name, staging_fd, temporary_name, backup_fd, backup_name,
            backup_inode, backup_digest, expected_inode, published_inode, deadline=deadline,
        )
        raise OSError("destination changed during atomic exchange; displaced entry quarantined")
    return published_inode, displaced


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    _directory_lock_held: bool = False,
    deadline: float | None = None,
    _directory_fd: int | None = None,
) -> tuple[object, ...] | None:
    """Atomically write JSON using a private descriptor-relative staging directory."""
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("atomic JSON publication deadline exceeded")
    path = Path(path)
    if _directory_fd is not None:
        if isinstance(_directory_fd, bool) or not isinstance(_directory_fd, int) or _directory_fd < 0:
            raise ValueError("atomic publication directory descriptor is invalid")
        directory_fd = os.dup(_directory_fd)
    else:
        directory_fd = _open_directory_no_follow(path.parent, create=True)
    if not _directory_lock_held:
        try:
            _acquire_exclusive_lock(directory_fd, deadline=deadline)
        except BaseException:
            os.close(directory_fd)
            raise
    temporary_name = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    temporary_inode: tuple[object, ...] | None = None
    staging_name = ""
    staging_fd = -1
    staging_inode: tuple[object, ...] | None = None
    published_inode: tuple[object, ...] | None = None
    backup_name = ""
    backup_inode: tuple[object, ...] | None = None
    backup_digest: str | None = None
    backup_fd = -1
    existing_present = False
    existing_inode: tuple[object, ...] | None = None
    cleanup_error: Exception | None = None
    cleanup_deadline = deadline
    temporary_cleanup_failed = False
    try:
        try:
            existing_stat = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(existing_stat.st_mode) or not stat.S_ISREG(existing_stat.st_mode):
                raise ValueError(f"Refusing to atomically replace non-regular JSON file: {path}")
            existing_mode = stat.S_IMODE(existing_stat.st_mode) & 0o660
            existing_inode = _cleanup_identity(existing_stat)
            if existing_mode == 0:
                existing_mode = 0o600
            existing_present = True
        except FileNotFoundError:
            existing_mode = None
        if path.name == "jobs.json":
            existing_mode = 0o660
        serialized = json.dumps(payload, indent=2, allow_nan=False)
        if len(serialized.encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError(f"JSON payload exceeds the {MAX_JSON_BYTES}-byte limit")
        staging_name, staging_fd, captured_staging_inode = cast(
            tuple[str, int, tuple[int, int]],
            _open_private_staging_directory(
                directory_fd, include_identity=True
            ),
        )
        staging_inode = (captured_staging_inode[0], captured_staging_inode[1], "held-directory")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=staging_fd,
        )
        temporary_stat = os.fstat(descriptor)
        temporary_inode = _cleanup_identity(temporary_stat)
        if existing_present:
            assert existing_inode is not None
            backup_name = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.backup"
            source_fd = -1
            try:
                source_fd = os.open(
                    path.name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                source_before = os.fstat(source_fd)
                if not _identity_matches(_cleanup_identity(source_before), existing_inode):
                    raise OSError("destination changed while opening the atomic backup source")
                backup_fd = os.open(
                    backup_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    existing_mode or 0o600,
                    dir_fd=staging_fd,
                )
                source_digest = hashlib.sha256()
                copied = 0
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("atomic backup deadline exceeded")
                    chunk = os.read(source_fd, min(64 * 1024, MAX_JSON_BYTES + 1 - copied))
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_JSON_BYTES:
                        raise ValueError(f"atomic backup exceeds the {MAX_JSON_BYTES}-byte limit")
                    source_digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(backup_fd, view)
                        if written <= 0:
                            raise OSError("atomic backup write made no progress")
                        view = view[written:]
                os.fsync(backup_fd)
                source_after = os.fstat(source_fd)
                if not _identity_matches(_cleanup_identity(source_after), existing_inode):
                    raise OSError("destination changed while copying the atomic backup")
                backup_digest = source_digest.hexdigest()
                if _regular_content_digest_descriptor(source_fd, deadline=deadline) != backup_digest:
                    raise OSError("destination content changed while copying the atomic backup")
                backup_inode = _cleanup_identity(os.fstat(backup_fd))
                if backup_inode is None:
                    raise OSError("atomic backup identity is unavailable")
            finally:
                if source_fd >= 0:
                    os.close(source_fd)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            json.dump(payload, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
            if existing_mode is not None:
                os.fchmod(handle.fileno(), existing_mode)
        temporary_inode = _cleanup_identity(os.fstat(descriptor))
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("atomic JSON publication deadline exceeded")
        if existing_present:
            assert existing_inode is not None and temporary_inode is not None
            published_inode, temporary_inode = _exchange_existing_publication(
                directory_fd,
                path.name,
                staging_fd,
                temporary_name,
                temporary_inode,
                existing_inode,
                backup_fd,
                backup_name,
                backup_inode,
                backup_digest,
                deadline=deadline,
            )
        else:
            _rename_noreplace(staging_fd, temporary_name, directory_fd, path.name)
            temporary_name = ""
            published_inode = _cleanup_identity(os.lstat(path.name, dir_fd=directory_fd))
        try:
            os.fsync(directory_fd)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("atomic JSON publication deadline exceeded")
            if _directory_fd is not None:
                published_fd = os.open(
                    path.name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            else:
                published_fd = _open_regular_descriptor(path)
            try:
                if _parse_strict_json(_read_bounded_utf8(published_fd, label="published JSON", deadline=deadline)) != payload:
                    raise ValueError("atomic JSON publication did not match the trusted payload")
                final_published_stat = os.lstat(path.name, dir_fd=directory_fd)
                if not _identity_matches(_cleanup_identity(final_published_stat), published_inode):
                    raise OSError("atomic JSON publication changed during readback")
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("atomic JSON publication deadline exceeded")
            finally:
                if published_fd >= 0:
                    os.close(published_fd)
        except Exception as primary:
            rolled_back_via_exchange = _rollback_preserving_primary(
                primary, directory_fd, path.name, staging_fd, temporary_name, backup_fd, backup_name,
                backup_inode, backup_digest, existing_inode, published_inode, deadline=deadline,
            )
            if rolled_back_via_exchange:
                temporary_inode = published_inode
            raise
        if backup_name and backup_inode is not None:
            _remove_entry_at(staging_fd, backup_name, backup_inode, deadline=cleanup_deadline, verify_preclaim_ctime=False)
            if backup_fd >= 0:
                os.close(backup_fd)
                backup_fd = -1
            backup_name = ""
            backup_inode = None
        return published_inode
    finally:
        cleanup_deadline = deadline
        if deadline is not None:
            cleanup_deadline = max(deadline, time.monotonic() + PUBLICATION_CLEANUP_GRACE_SECONDS)
        primary_error = sys.exc_info()[1]
        if backup_fd >= 0:
            try:
                os.close(backup_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if staging_fd >= 0:
            if temporary_inode is not None:
                try:
                    _remove_entry_at(staging_fd, temporary_name, temporary_inode, deadline=cleanup_deadline, verify_preclaim_ctime=False)
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    cleanup_error = exc
                    temporary_cleanup_failed = True
            os.close(staging_fd)
        if descriptor >= 0:
            os.close(descriptor)
        if staging_name and staging_inode is not None and not temporary_cleanup_failed:
            try:
                _remove_entry_at(directory_fd, staging_name, staging_inode, deadline=cleanup_deadline)
            except FileNotFoundError:
                pass
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if not _directory_lock_held:
            fcntl.flock(directory_fd, fcntl.LOCK_UN)
        os.close(directory_fd)
        if cleanup_error is not None and primary_error is None:
            raise cleanup_error


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    _directory_lock_held: bool = False,
    deadline: float | None = None,
    _directory_fd: int | None = None,
) -> tuple[object, ...] | None:
    """Atomically publish bounded UTF-8 text from a private staging directory."""
    if not isinstance(content, str):
        raise TypeError("atomic text content must be a string")
    if len(content.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError(f"text payload exceeds the {MAX_JSON_BYTES}-byte limit")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("atomic text publication deadline exceeded")
    path = Path(path)
    if _directory_fd is not None:
        if isinstance(_directory_fd, bool) or not isinstance(_directory_fd, int) or _directory_fd < 0:
            raise ValueError("atomic publication directory descriptor is invalid")
        directory_fd = os.dup(_directory_fd)
    else:
        directory_fd = _open_directory_no_follow(path.parent, create=True)
    if not _directory_lock_held:
        try:
            _acquire_exclusive_lock(directory_fd, deadline=deadline)
        except BaseException:
            os.close(directory_fd)
            raise
    temporary_name = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    temporary_inode: tuple[object, ...] | None = None
    staging_name = ""
    staging_fd = -1
    staging_inode: tuple[object, ...] | None = None
    published_inode: tuple[object, ...] | None = None
    backup_name = ""
    backup_inode: tuple[object, ...] | None = None
    backup_digest: str | None = None
    backup_fd = -1
    existing_present = False
    existing_inode: tuple[object, ...] | None = None
    cleanup_error: Exception | None = None
    cleanup_deadline = deadline
    temporary_cleanup_failed = False
    try:
        staging_name, staging_fd, captured_staging_inode = cast(
            tuple[str, int, tuple[int, int]],
            _open_private_staging_directory(
                directory_fd, include_identity=True
            ),
        )
        staging_inode = (captured_staging_inode[0], captured_staging_inode[1], "held-directory")
        try:
            existing_stat = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(existing_stat.st_mode) or not stat.S_ISREG(existing_stat.st_mode):
                raise ValueError(f"Refusing to atomically replace non-regular text file: {path}")
            existing_present = True
            existing_inode = _cleanup_identity(existing_stat)
        except FileNotFoundError:
            pass
        descriptor = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=staging_fd)
        temporary_stat = os.fstat(descriptor)
        temporary_inode = _cleanup_identity(temporary_stat)
        if existing_present:
            assert existing_inode is not None
            backup_name = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.backup"
            source_fd = -1
            try:
                source_fd = os.open(
                    path.name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                source_before = os.fstat(source_fd)
                if not _identity_matches(_cleanup_identity(source_before), existing_inode):
                    raise OSError("destination changed while opening the atomic backup source")
                backup_fd = os.open(
                    backup_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=staging_fd,
                )
                source_digest = hashlib.sha256()
                copied = 0
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("atomic backup deadline exceeded")
                    chunk = os.read(source_fd, min(64 * 1024, MAX_JSON_BYTES + 1 - copied))
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_JSON_BYTES:
                        raise ValueError(f"atomic backup exceeds the {MAX_JSON_BYTES}-byte limit")
                    source_digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(backup_fd, view)
                        if written <= 0:
                            raise OSError("atomic backup write made no progress")
                        view = view[written:]
                os.fsync(backup_fd)
                source_after = os.fstat(source_fd)
                if not _identity_matches(_cleanup_identity(source_after), existing_inode):
                    raise OSError("destination changed while copying the atomic backup")
                backup_digest = source_digest.hexdigest()
                if _regular_content_digest_descriptor(source_fd, deadline=deadline) != backup_digest:
                    raise OSError("destination content changed while copying the atomic backup")
                backup_inode = _cleanup_identity(os.fstat(backup_fd))
                if backup_inode is None:
                    raise OSError("atomic backup identity is unavailable")
            finally:
                if source_fd >= 0:
                    os.close(source_fd)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("atomic text publication deadline exceeded")
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_inode = _cleanup_identity(os.fstat(descriptor))
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("atomic text publication deadline exceeded")
        if existing_present:
            assert existing_inode is not None and temporary_inode is not None
            published_inode, temporary_inode = _exchange_existing_publication(
                directory_fd,
                path.name,
                staging_fd,
                temporary_name,
                temporary_inode,
                existing_inode,
                backup_fd,
                backup_name,
                backup_inode,
                backup_digest,
                deadline=deadline,
            )
        else:
            _rename_noreplace(staging_fd, temporary_name, directory_fd, path.name)
            temporary_name = ""
            published_inode = _cleanup_identity(os.lstat(path.name, dir_fd=directory_fd))
        try:
            os.fsync(directory_fd)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("atomic text publication deadline exceeded")
            if _directory_fd is not None:
                published_fd = os.open(
                    path.name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            else:
                published_fd = _open_regular_descriptor(path)
            try:
                if _read_bounded_utf8(published_fd, deadline=deadline, label="published text") != content:
                    raise ValueError("atomic text publication did not match the trusted content")
                final_published_stat = os.lstat(path.name, dir_fd=directory_fd)
                if not _identity_matches(_cleanup_identity(final_published_stat), published_inode):
                    raise OSError("atomic text publication changed during readback")
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("atomic text publication deadline exceeded")
            finally:
                if published_fd >= 0:
                    os.close(published_fd)
        except Exception as primary:
            rolled_back_via_exchange = _rollback_preserving_primary(
                primary, directory_fd, path.name, staging_fd, temporary_name, backup_fd, backup_name,
                backup_inode, backup_digest, existing_inode, published_inode, deadline=deadline,
            )
            if rolled_back_via_exchange:
                temporary_inode = published_inode
            raise
        if backup_name and backup_inode is not None:
            _remove_entry_at(staging_fd, backup_name, backup_inode, deadline=cleanup_deadline, verify_preclaim_ctime=False)
            if backup_fd >= 0:
                os.close(backup_fd)
                backup_fd = -1
            backup_name = ""
            backup_inode = None
        return published_inode
    finally:
        cleanup_deadline = deadline
        if deadline is not None:
            cleanup_deadline = max(deadline, time.monotonic() + PUBLICATION_CLEANUP_GRACE_SECONDS)
        primary_error = sys.exc_info()[1]
        if backup_fd >= 0:
            try:
                os.close(backup_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if staging_fd >= 0:
            if temporary_inode is not None:
                try:
                    _remove_entry_at(staging_fd, temporary_name, temporary_inode, deadline=cleanup_deadline, verify_preclaim_ctime=False)
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    cleanup_error = exc
                    temporary_cleanup_failed = True
            os.close(staging_fd)
        if descriptor >= 0:
            os.close(descriptor)
        if staging_name and staging_inode is not None and not temporary_cleanup_failed:
            try:
                _remove_entry_at(directory_fd, staging_name, staging_inode, deadline=cleanup_deadline)
            except FileNotFoundError:
                pass
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if not _directory_lock_held:
            fcntl.flock(directory_fd, fcntl.LOCK_UN)
        os.close(directory_fd)
        if cleanup_error is not None and primary_error is None:
            raise cleanup_error


def _read_json_object_unlocked(path: Path, deadline: float | None = None) -> dict:
    """Read a regular JSON object through an O_NOFOLLOW descriptor."""
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("JSON artifact read deadline exceeded")
    try:
        descriptor = _open_regular_descriptor(path)
    except FileNotFoundError:
        return {}
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"Expected regular JSON file in {path}")
        content = _read_bounded_utf8(descriptor, deadline=deadline, label=f"JSON artifact {path}").strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not content:
        return {}
    try:
        loaded = _parse_strict_json(content)
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




def _flock_with_deadline(lock_file: Any, operation: int, deadline: float | None) -> None:
    if deadline is None:
        fcntl.flock(lock_file, operation)
        return
    while True:
        try:
            fcntl.flock(lock_file, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("JSON lock deadline exceeded")
            time.sleep(min(0.01, remaining))


def read_json_object(path: str | Path, *, lock: bool = True, deadline: float | None = None) -> dict:
    """Read a JSON object, optionally under the advisory write lock."""
    path = Path(path)
    if not lock:
        return _read_json_object_unlocked(path, deadline)
    lock_path = path.with_name(f"{path.name}.lock")
    with _open_lock_file(lock_path) as lock_file:
        _flock_with_deadline(lock_file, fcntl.LOCK_SH, deadline)
        try:
            return _read_json_object_unlocked(path, deadline)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def read_json_artifact(path: str | Path, *, deadline: float | None = None) -> dict:
    """Read an artifact JSON object without creating a persistent lock file."""
    return _read_json_object_unlocked(Path(path), deadline)


def read_text_artifact(path: str | Path) -> str:
    """Read a regular text artifact through a no-follow descriptor."""
    try:
        descriptor = _open_regular_descriptor(path)
    except FileNotFoundError as exc:
        raise ValueError(f"Text artifact is missing: {path}") from exc
    try:
        return _read_bounded_utf8(descriptor, label=f"Text artifact {path}")
    finally:
        os.close(descriptor)


def update_json_file(path: str | Path, updater: Callable[[dict], dict | None]) -> dict:
    """Load/mutate/write a JSON object under the shared publication lock.

    The directory lock is shared with atomic_write_json(), so direct atomic
    writers cannot publish between this helper's read and replacement.
    """
    path = Path(path)
    lock_path = path.with_name(f"{path.name}.lock")
    directory_fd = _open_directory_no_follow(path.parent, create=True)
    try:
        with _open_lock_file(lock_path) as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
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
                atomic_write_json(path, payload, _directory_lock_held=True)
                return payload
            finally:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
                fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        os.close(directory_fd)


def _open_regular_under_root(root: Path, path: Path) -> int:
    """Open a contained regular file by walking held no-follow descriptors."""
    root = Path(root)
    path = Path(path)
    relative = path.relative_to(root)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError("invalid rooted artifact path")
    directory_fd = _open_directory_no_follow(root, create=False)
    try:
        pinned_root = os.fstat(directory_fd)
        current_root = os.lstat(root)
        if (current_root.st_dev, current_root.st_ino) != (pinned_root.st_dev, pinned_root.st_ino):
            raise OSError("artifact root changed during rooted open")
        current_fd = directory_fd
        descriptor = -1
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
                raise OSError("rooted artifact is not a regular file")
            current_root = os.lstat(root)
            if (current_root.st_dev, current_root.st_ino) != (pinned_root.st_dev, pinned_root.st_ino):
                raise OSError("artifact root changed during rooted open")
            return descriptor
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
                descriptor = -1
            raise
        finally:
            for owned_fd in reversed(owned_directory_fds):
                os.close(owned_fd)
    finally:
        os.close(directory_fd)


def _safe_regular_file(root: Path, path: Path, deadline: float | None = None) -> bool:
    """Accept only regular files whose complete path stays inside ``root``."""
    if deadline is not None and time.monotonic() >= deadline:
        return False
    try:
        descriptor = _open_regular_under_root(Path(root), Path(path))
        os.close(descriptor)
        return deadline is None or time.monotonic() < deadline
    except (OSError, ValueError):
        return False


def _has_file(root: Path, relative: str, deadline: float | None = None) -> bool:
    if deadline is not None and time.monotonic() >= deadline:
        return False
    path = root / relative
    try:
        descriptor = _open_regular_under_root(root, path)
        try:
            return os.fstat(descriptor).st_size > 0 and (deadline is None or time.monotonic() < deadline)
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return False


def _open_regular_descriptor(path: str | Path) -> int:
    """Open one regular file through no-follow parent descriptors."""
    path = Path(path)
    directory_fd = _open_directory_no_follow(path.parent, create=False)
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("media artifact is not a regular file")
        return descriptor
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        raise
    finally:
        os.close(directory_fd)


def _read_secure_text_file(path: str | Path, max_bytes: int) -> str:
    """Read bounded UTF-8 text from a descriptor-bound regular file."""
    descriptor = _open_regular_descriptor(path)
    try:
        file_stat = os.fstat(descriptor)
        if file_stat.st_mode & 0o077:
            raise OSError("authentication file permissions are too broad")
        raw_value = os.read(descriptor, max_bytes + 1)
        if len(raw_value) > max_bytes:
            raise OSError("authentication file is too large")
        return raw_value.decode("utf-8")
    finally:
        os.close(descriptor)


def _read_prefix_no_follow(path: str | Path, size: int) -> bytes:
    descriptor = _open_regular_descriptor(path)
    try:
        return os.read(descriptor, size)
    finally:
        os.close(descriptor)


def _probe_media(path: Path, *, _descriptor: int | None = None, deadline: float | None = None) -> dict[str, Any]:
    """Return conservative media-file verification details."""
    result: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "size_bytes": 0,
        "ffprobe_checked": False,
        "valid": False,
    }
    descriptor_owned = _descriptor is None
    if descriptor_owned:
        try:
            descriptor = _open_regular_descriptor(path)
        except OSError as exc:
            result["error"] = f"media file is unavailable: {exc.__class__.__name__}"
            return result
    else:
        descriptor = _descriptor
    try:
        file_stat = os.fstat(descriptor)
        result["exists"] = stat.S_ISREG(file_stat.st_mode)
        result["size_bytes"] = file_stat.st_size if result["exists"] else 0
        if not result["exists"] or result["size_bytes"] <= 0:
            return result
        if result["size_bytes"] > MAX_MEDIA_HASH_BYTES:
            result["error"] = "media file exceeds verification size limit"
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
        if deadline is not None and time.monotonic() >= deadline:
            result["error"] = "media verification deadline exceeded"
            return result

        result["ffprobe_checked"] = True
        probe_command = [ffprobe, "-v", "error",
                         "-show_entries", "stream=codec_type,width,height:format=duration", "-of", "json",
                         f"/proc/self/fd/{descriptor}"]
        probe_timeout = 15.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result["error"] = "media verification deadline exceeded"
                return result
            probe_timeout = min(probe_timeout, remaining)
        try:
            proc = _run_bounded_subprocess(
                probe_command,
                timeout=probe_timeout,
                pass_fds=(descriptor,),
                deadline=deadline,
            )
        except subprocess.TimeoutExpired:
            result["error"] = "ffprobe timed out"
            return result
        except subprocess.SubprocessError:
            result["error"] = "ffprobe supervision failed"
            return result
        except OSError:
            result["error"] = "ffprobe failed to start"
            return result
        if proc.returncode != 0:
            result["error"] = "ffprobe rejected media"
            return result
        if deadline is not None and time.monotonic() >= deadline:
            result["error"] = "media verification deadline exceeded"
            return result
        try:
            payload = _parse_strict_json(proc.stdout)
            if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list) or not isinstance(payload.get("format"), dict):
                raise ValueError("invalid ffprobe output")
            expected_type = "video" if is_mp4 else "audio"
            streams = [stream for stream in payload["streams"] if isinstance(stream, dict)]
            if not any(stream.get("codec_type") == expected_type for stream in streams):
                result["error"] = f"{expected_type} artifact has no {expected_type} stream"
                return result
            if not is_mp4 and any(stream.get("codec_type") == "video" for stream in streams):
                result["error"] = "audio artifact contains an unexpected video stream"
                return result
            duration = float(payload["format"].get("duration", "nan"))
            if is_mp4:
                video = next(stream for stream in streams if stream.get("codec_type") == "video")
                raw_width = video.get("width")
                raw_height = video.get("height")
                if (
                    isinstance(raw_width, bool)
                    or not isinstance(raw_width, (int, str))
                    or isinstance(raw_height, bool)
                    or not isinstance(raw_height, (int, str))
                ):
                    raise ValueError("invalid video dimensions")
                width = int(raw_width)
                height = int(raw_height)
                if width <= 0 or height <= 0:
                    raise ValueError("invalid video dimensions")
                result["width"] = width
                result["height"] = height
        except (TypeError, ValueError, json.JSONDecodeError):
            result["error"] = "invalid media ffprobe output"
            return result
        if not math.isfinite(duration):
            result["error"] = "media duration is not finite"
            return result
        result["duration_seconds"] = duration
        if deadline is not None and time.monotonic() >= deadline:
            result["error"] = "media verification deadline exceeded"
            return result
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        hashed_bytes = 0
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            hashed_bytes += len(chunk)
            if hashed_bytes > MAX_MEDIA_HASH_BYTES:
                result["error"] = "media file exceeds verification size limit"
                return result
            if deadline is not None and time.monotonic() >= deadline:
                result["error"] = "media verification deadline exceeded"
                return result
            digest.update(chunk)
        result["sha256"] = digest.hexdigest()
        if deadline is not None and time.monotonic() >= deadline:
            result["error"] = "media verification deadline exceeded"
            result["sha256"] = None
            return result
        result["valid"] = duration > 0
        return result
    finally:
        if descriptor_owned:
            os.close(descriptor)


def _storyboard_info(root: Path, deadline: float | None = None) -> dict[str, Any]:
    sb = root / "storyboard.json"
    result: dict[str, Any] = {"expected_scenes": 0, "scene_numbers": [], "scene_durations": {}, "total_duration": None, "errors": [], "valid": False, "profile": None}
    if deadline is not None and time.monotonic() >= deadline:
        result["errors"].append("storyboard deadline exceeded")
        return result
    if not _safe_regular_file(root, sb, deadline):
        return result
    try:
        data = read_json_artifact(sb, deadline=deadline)
    except ValueError as exc:
        if "Expected JSON object" in str(exc):
            result["errors"].append("storyboard.json must be an object")
        else:
            result["errors"].append(f"storyboard.json invalid or unreadable: {exc}")
        return result
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TimeoutError) as exc:
        result["errors"].append(f"storyboard.json invalid or unreadable: {exc}")
        return result
    if not isinstance(data, dict):
        result["errors"].append(f"storyboard.json must be an object, got {type(data).__name__}")
        return result
    try:
        result["profile"] = validate_output_profile_contract(data, "storyboard")
    except ValueError as exc:
        result["errors"].append(str(exc))
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
    if deadline is not None and time.monotonic() >= deadline:
        result["errors"].append("storyboard deadline exceeded")
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
    raw_total_duration = data.get("total_duration")
    if not isinstance(raw_total_duration, bool) and isinstance(raw_total_duration, (int, float)):
        total_duration = float(raw_total_duration)
        if math.isfinite(total_duration) and total_duration > 0:
            result["total_duration"] = total_duration
    scene_durations: dict[int, float] = {}
    for scene in scenes:
        if deadline is not None and time.monotonic() >= deadline:
            result["errors"].append("storyboard deadline exceeded")
            return result
        raw_duration = scene.get("duration_seconds")
        if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)):
            continue
        duration = float(raw_duration)
        if math.isfinite(duration) and duration > 0:
            scene_durations[scene["scene_number"]] = duration
    result["scene_durations"] = scene_durations
    result["valid"] = True
    result["scene_numbers"] = list(range(1, len(scenes) + 1))
    return result


def _generation_plan_info(root: Path, deadline: float | None = None) -> dict[str, Any]:
    """Validate the generation plan and expose its scene identity."""
    plan_path = root / "clips" / "generation_plan.json"
    result: dict[str, Any] = {"scene_numbers": [], "scene_hashes": {}, "scene_durations": {}, "errors": [], "valid": False, "status": None, "profile": None}
    if deadline is not None and time.monotonic() >= deadline:
        result["errors"].append("generation plan deadline exceeded")
        return result
    if not _safe_regular_file(root, plan_path, deadline):
        return result
    try:
        plan = read_json_artifact(plan_path, deadline=deadline)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        result["errors"].append(f"generation_plan.json invalid or unreadable: {exc}")
        return result
    try:
        result["profile"] = validate_output_profile_contract(plan, "generation plan")
    except ValueError as exc:
        result["errors"].append(str(exc))
        return result
    scenes = plan.get("scenes")
    raw_plan_status = plan.get("status")
    plan_status = (
        raw_plan_status
        if isinstance(raw_plan_status, str) and raw_plan_status in {"completed", "dry_run"}
        else None
    )
    result["status"] = None
    total_scenes = plan.get("total_scenes")
    if deadline is not None and time.monotonic() >= deadline:
        result["errors"].append("generation plan deadline exceeded")
        return result
    scene_statuses = [scene.get("status") for scene in scenes if isinstance(scene, dict)] if isinstance(scenes, list) else []
    if (
        not isinstance(plan_status, str)
        or plan_status not in {"completed", "dry_run"}
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
        or any(not isinstance(status, str) or status not in {"downloaded", "verified", "dry_run"} for status in scene_statuses)
        or (
            (plan_status == "completed" and any(status not in {"downloaded", "verified"} for status in scene_statuses))
            or (plan_status == "dry_run" and any(status != "dry_run" for status in scene_statuses))
        )
    ):
        result["errors"].append("generation_plan.json has invalid scene coverage.")
        return result
    result["status"] = plan_status
    scene_numbers = [scene.get("scene_number") for scene in scenes]
    if deadline is not None and time.monotonic() >= deadline:
        result["errors"].append("generation plan deadline exceeded")
        return result
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
        scene_durations: dict[int, float] = {}
        for scene in scenes:
            if deadline is not None and time.monotonic() >= deadline:
                result["errors"].append("generation plan deadline exceeded")
                return result
            digest = scene.get("sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                result["errors"].append("completed generation plan is missing a valid scene sha256.")
                return result
            scene_hashes[scene["scene_number"]] = digest
            raw_duration = scene.get("duration_seconds")
            if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)):
                result["errors"].append("completed generation plan is missing a valid scene duration.")
                return result
            duration = float(raw_duration)
            if not math.isfinite(duration) or duration <= 0:
                result["errors"].append("completed generation plan scene durations must be finite and positive.")
                return result
            scene_durations[scene["scene_number"]] = duration
        result["scene_hashes"] = scene_hashes
        result["scene_durations"] = scene_durations
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


def _sha256_regular_file(path: Path, *, root: Path | None = None, deadline: float | None = None) -> str | None:
    descriptor = -1
    try:
        descriptor = _open_regular_under_root(root, path) if root is not None else _open_regular_descriptor(path)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_MEDIA_HASH_BYTES:
            return None
        digest = hashlib.sha256()
        hashed_bytes = 0
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            hashed_bytes += len(chunk)
            if hashed_bytes > MAX_MEDIA_HASH_BYTES:
                return None
            if deadline is not None and time.monotonic() >= deadline:
                return None
            digest.update(chunk)
        if deadline is not None and time.monotonic() >= deadline:
            return None
        final_stat = os.fstat(descriptor)
        if (final_stat.st_dev, final_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino) or final_stat.st_size != file_stat.st_size:
            return None
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _valid_clip_files(
    root: Path,
    expected_hashes: dict[int, str] | None = None,
    expected_profile: dict[str, Any] | None = None,
    expected_durations: dict[int, float] | None = None,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    clips_dir = root / "clips"
    try:
        clips_directory_fd = _open_directory_no_follow(clips_dir, create=False)
    except (OSError, ValueError):
        return []
    verified = []
    try:
        for name in sorted(os.listdir(clips_directory_fd)):
            if deadline is not None and time.monotonic() >= deadline:
                break
            if not name.startswith("scene_") or not name.endswith(".mp4"):
                continue
            scene_number = _clip_scene_number(name)
            if scene_number is None:
                continue
            descriptor = -1
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=clips_directory_fd,
                )
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    continue
                info = _probe_media(clips_dir / name, _descriptor=descriptor, deadline=deadline)
                if info.get("valid"):
                    if expected_profile is not None and (
                        info.get("width") != expected_profile.get("width")
                        or info.get("height") != expected_profile.get("height")
                    ):
                        continue
                    expected_duration = expected_durations.get(scene_number) if expected_durations is not None else None
                    if expected_durations is not None and (
                        expected_duration is None
                        or abs(info.get("duration_seconds", 0) - expected_duration) > 0.5
                    ):
                        continue
                    if expected_hashes is not None and info.get("sha256") != expected_hashes.get(scene_number):
                        continue
                    info["scene_number"] = scene_number
                    verified.append(info)
            except OSError:
                continue
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
    finally:
        os.close(clips_directory_fd)
    return verified


def _clip_file_coverage(root: Path, deadline: float | None = None) -> tuple[set[int], set[int], list[str]]:
    """Inspect every scene clip filename, including invalid artifacts."""
    clips_dir = root / "clips"
    scene_numbers: list[int] = []
    invalid_files: list[str] = []
    if deadline is not None and time.monotonic() >= deadline:
        return set(), set(), []
    if clips_dir.is_symlink() or not clips_dir.is_dir():
        return set(), set(), []
    for path in sorted(clips_dir.iterdir()):
        if deadline is not None and time.monotonic() >= deadline:
            return set(), set(), invalid_files
        if path.name == "generation_plan.json" or path.name.startswith(".provider-publication-"):
            continue
        if not _safe_regular_file(root, path, deadline):
            invalid_files.append(path.name)
            continue
        scene_number = _clip_scene_number(path.name)
        if scene_number is None:
            invalid_files.append(path.name)
            continue
        scene_numbers.append(scene_number)
    duplicates = {number for number in scene_numbers if scene_numbers.count(number) > 1}
    return set(scene_numbers), duplicates, invalid_files


def _has_visual_files(root: Path, deadline: float | None = None) -> bool:
    if deadline is not None and time.monotonic() >= deadline:
        return False
    visuals_fd = -1
    try:
        visuals_fd = _open_directory_no_follow(root / "visuals", create=False)
    except OSError:
        return False
    try:
        for name in os.listdir(visuals_fd):
            if deadline is not None and time.monotonic() >= deadline:
                return False
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
    expected_final_duration_seconds: float | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Compute package status from actual output artifacts.

    Returns a JSON-serializable summary. The status field is intentionally more
    precise than job.status; `completed` can still mean only an editor package.
    """
    root = Path(root)
    if job_status is not None and not isinstance(job_status, str):
        job_status = None
    identity_errors: list[str] = []
    if _safe_regular_file(root, root / ".source-artifact.invalid", deadline):
        identity_errors.append("source artifacts are invalid; refresh required")
    provenance: dict[str, Any] = {}
    identity_required = expected_run_id != LEGACY_FLAT_RUN_ID
    if identity_required and any(value is not None for value in (expected_job_id, expected_attempt, expected_run_id, expected_source_digest)):
        try:
            provenance = read_json_artifact(root / "run_provenance.json", deadline=deadline)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, TimeoutError) as exc:
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

    storyboard = _storyboard_info(root, deadline) if not identity_errors else {"expected_scenes": 0, "scene_numbers": [], "scene_durations": {}, "total_duration": None, "errors": [], "valid": False, "profile": None}
    generation_plan = _generation_plan_info(root, deadline) if not identity_errors else {"scene_numbers": [], "scene_hashes": {}, "scene_durations": {}, "errors": [], "valid": False, "status": None, "profile": None}
    expected_scenes = int(storyboard["expected_scenes"])
    timing_sum_matches = (
        isinstance(storyboard.get("total_duration"), (int, float))
        and not isinstance(storyboard.get("total_duration"), bool)
        and math.isfinite(float(storyboard["total_duration"]))
        and bool(storyboard.get("scene_durations"))
        and abs(sum(storyboard["scene_durations"].values()) - float(storyboard["total_duration"])) <= 0.5
    )
    provider_contract_ready = (
        generation_plan.get("status") == "completed"
        and bool(generation_plan.get("scene_durations"))
        and bool(storyboard.get("scene_durations"))
        and timing_sum_matches
    )
    valid_clips = _valid_clip_files(
        root,
        generation_plan.get("scene_hashes") if provider_contract_ready else None,
        storyboard.get("profile") if provider_contract_ready else None,
        storyboard.get("scene_durations") if provider_contract_ready else None,
        deadline,
    )
    clip_scene_numbers = {clip["scene_number"] for clip in valid_clips if isinstance(clip.get("scene_number"), int)}
    all_clip_scene_numbers, duplicate_clip_scene_numbers, invalid_clip_files = _clip_file_coverage(root, deadline)
    expected_clip_numbers = set(range(1, expected_scenes + 1))
    missing_clip_numbers = sorted(expected_clip_numbers - clip_scene_numbers)
    extra_clip_numbers = sorted(all_clip_scene_numbers - expected_clip_numbers)
    matching_generation_plan = (
        bool(storyboard["valid"])
        and bool(generation_plan["valid"])
        and generation_plan["scene_numbers"] == storyboard["scene_numbers"]
    )
    matching_output_profile = (
        bool(storyboard["valid"])
        and bool(generation_plan["valid"])
        and storyboard.get("profile") == generation_plan.get("profile")
    )
    matching_scene_durations = (
        provider_contract_ready
        and storyboard.get("scene_durations") == generation_plan.get("scene_durations")
    )
    plan_sha256 = _sha256_regular_file(root / "clips" / "generation_plan.json", root=root, deadline=deadline) if generation_plan["valid"] else None
    plan_identity_matches = (
        isinstance(expected_plan_sha256, str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256))
        and plan_sha256 == expected_plan_sha256
    )
    complete_clip_coverage = (
        bool(expected_scenes)
        and matching_generation_plan
        and matching_output_profile
        and matching_scene_durations
        and generation_plan.get("status") == "completed"
        and not missing_clip_numbers
        and not extra_clip_numbers
        and not duplicate_clip_scene_numbers
        and not invalid_clip_files
        and len(valid_clips) == expected_scenes
    )
    final_path = root / "final" / "video.mp4"
    final_video = _probe_media(final_path, deadline=deadline) if _safe_regular_file(root, final_path, deadline) else {
        "path": str(final_path),
        "exists": False,
        "size_bytes": 0,
        "ffprobe_checked": False,
        "valid": False,
        "error": "final media path contains a symlink or non-regular file",
    }
    observed_final_sha256 = final_video.get("sha256") if final_video.get("valid", False) else None
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
    final_video_profile_matches = bool(
        final_video.get("valid", False)
        and isinstance(storyboard.get("profile"), dict)
        and final_video.get("width") == storyboard["profile"].get("width")
        and final_video.get("height") == storyboard["profile"].get("height")
    )
    final_video_duration_matches = bool(
        final_video.get("valid", False)
        and isinstance(storyboard.get("total_duration"), (int, float))
        and not isinstance(storyboard.get("total_duration"), bool)
        and math.isfinite(float(storyboard["total_duration"]))
        and abs(final_video.get("duration_seconds", 0) - float(storyboard["total_duration"])) <= 0.5
    )
    final_evidence_duration_matches = (
        expected_final_duration_seconds is None
        or (
            isinstance(expected_final_duration_seconds, (int, float))
            and not isinstance(expected_final_duration_seconds, bool)
            and math.isfinite(float(expected_final_duration_seconds))
            and float(expected_final_duration_seconds) > 0.0
            and float(expected_final_duration_seconds) <= 86400.0
            and final_video.get("valid", False)
            and abs(final_video.get("duration_seconds", 0) - float(expected_final_duration_seconds)) <= 0.5
        )
    )
    deadline_exceeded = deadline is not None and time.monotonic() >= deadline
    final_video_ready = bool(
        not deadline_exceeded
        and not identity_errors
        and
        final_video.get("valid", False)
        and final_identity_matches
        and plan_identity_matches
        and complete_clip_coverage
        and final_video_profile_matches
        and final_video_duration_matches
        and final_evidence_duration_matches
        and job_status in {"completed", "editor_package"}
    )

    artifacts = {
        "creative_brief": _has_file(root, "creative_brief.json", deadline),
        "script": _has_file(root, "script.txt", deadline),
        "storyboard": _has_file(root, "storyboard.json", deadline) and bool(storyboard["valid"]),
        "visual_prompts": _has_file(root, "visual_prompts.json", deadline),
        "video_prompts": _has_file(root, "video_prompts.json", deadline),
        "voiceover_script": _has_file(root, "audio/voiceover_script.txt", deadline),
        "voiceover_audio": (
            _probe_media(root / "audio" / "voiceover.mp3", deadline=deadline).get("valid", False)
            if _safe_regular_file(root, root / "audio" / "voiceover.mp3", deadline)
            else False
        ),
        "music_prompt": _has_file(root, "music_prompt.txt", deadline),
        "captions": _has_file(root, "captions.srt", deadline),
        "assembly_manifest": _has_file(root, "assembly_manifest.json", deadline),
        "timeline_fcpxml": _has_file(root, "timeline.fcpxml", deadline),
        "generation_plan": bool(generation_plan["valid"]),
        "clips": len(valid_clips),
        "final_video": final_video_ready,
    }

    if deadline_exceeded:
        package_status = STATUS_NONE
    elif not identity_errors and job_status == STATUS_FAILED:
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
        "has_visuals": _has_visual_files(root, deadline),
        "has_voiceover": bool(artifacts["voiceover_audio"]),
        "has_clips": complete_clip_coverage,
        "has_final_video": bool(artifacts["final_video"]),
        "artifacts": artifacts,
        "artifact_errors": identity_errors + storyboard["errors"] + generation_plan["errors"] + (["package status deadline exceeded"] if deadline_exceeded else []),
        "final_video_probe": final_video,
        "final_video_sha256": final_sha256,
        "final_video_identity_matches": final_identity_matches,
        "final_video_profile_matches": final_video_profile_matches,
        "final_video_duration_matches": final_video_duration_matches,
        "final_evidence_duration_matches": final_evidence_duration_matches,
        "output_profile": storyboard.get("profile"),
        "generation_plan_sha256": plan_sha256,
        "generation_plan_identity_matches": plan_identity_matches,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _public_finite_number(value: Any, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        rendered = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(rendered) or not minimum <= rendered <= maximum:
        return None
    return rendered


def _public_summary(summary: dict[str, Any], root: Path) -> dict[str, Any]:
    """Return a strictly typed manifest-safe package summary."""
    if not isinstance(summary, dict):
        return {}

    safe: dict[str, Any] = {}
    valid_statuses = {
        STATUS_FAILED, STATUS_RESEARCH_ONLY, STATUS_SCRIPT_PACKAGE,
        STATUS_PROMPT_PACKAGE_ONLY, STATUS_EDITOR_PACKAGE,
        STATUS_CLIPS_GENERATED, STATUS_FINAL_VIDEO_READY, STATUS_NONE,
    }
    if isinstance(summary.get("package_status"), str) and summary["package_status"] in valid_statuses:
        safe["package_status"] = summary["package_status"]

    integer_fields = {
        "expected_scenes": (0, 10000),
        "verified_clips": (0, 10000),
    }
    for key, (minimum, maximum) in integer_fields.items():
        value = summary.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum:
            safe[key] = value

    list_fields = {"verified_clip_scene_numbers", "missing_clip_scene_numbers", "extra_clip_scene_numbers", "duplicate_clip_scene_numbers"}
    for key in list_fields:
        value = summary.get(key)
        if isinstance(value, list) and len(value) <= 10000 and all(isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10000 for item in value):
            safe[key] = sorted(value)

    for key in ("has_partial_clips", "has_visuals", "has_voiceover", "has_clips", "has_final_video", "final_video_identity_matches", "final_video_profile_matches", "final_video_duration_matches", "final_evidence_duration_matches", "generation_plan_identity_matches"):
        if isinstance(summary.get(key), bool):
            safe[key] = summary[key]

    artifacts = summary.get("artifacts")
    if isinstance(artifacts, dict):
        safe_artifacts: dict[str, Any] = {}
        allowed_artifacts = {"creative_brief", "script", "storyboard", "visual_prompts", "video_prompts", "voiceover_script", "voiceover_audio", "music_prompt", "captions", "assembly_manifest", "timeline_fcpxml", "generation_plan", "final_video"}
        for key in allowed_artifacts:
            if isinstance(artifacts.get(key), bool):
                safe_artifacts[key] = artifacts[key]
        if isinstance(artifacts.get("clips"), int) and not isinstance(artifacts["clips"], bool) and 0 <= artifacts["clips"] <= 10000:
            safe_artifacts["clips"] = artifacts["clips"]
        safe["artifacts"] = safe_artifacts

    errors = summary.get("artifact_errors")
    if isinstance(errors, list) and errors:
        safe["artifact_errors"] = ["artifact validation failed"]
    else:
        safe["artifact_errors"] = []

    probe = summary.get("final_video_probe")
    if isinstance(probe, dict):
        safe_probe: dict[str, Any] = {}
        raw_path = probe.get("path")
        if isinstance(raw_path, str) and len(raw_path) <= 512:
            try:
                relative = Path(raw_path).relative_to(root)
                if not relative.is_absolute() and ".." not in relative.parts and all(part not in {"", "."} for part in relative.parts):
                    safe_probe["path"] = str(relative)
            except (TypeError, ValueError):
                pass
        if isinstance(probe.get("size_bytes"), int) and not isinstance(probe["size_bytes"], bool) and 0 <= probe["size_bytes"] <= MAX_MEDIA_HASH_BYTES:
            safe_probe["size_bytes"] = probe["size_bytes"]
        if isinstance(probe.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", probe["sha256"]):
            safe_probe["sha256"] = probe["sha256"]
        safe_duration = _public_finite_number(probe.get("duration_seconds"), 0.0, 86400.0)
        if safe_duration is not None:
            safe_probe["duration_seconds"] = safe_duration
        if isinstance(probe.get("format_name"), str) and probe["format_name"] in {"mp4", "mov", "matroska", "webm", "mp3", "wav", "m4a", "ogg", "flac", "mov,mp4,m4a,3gp,3g2,mj2"}:
            safe_probe["format_name"] = probe["format_name"]
        for key in ("has_audio", "has_video"):
            if isinstance(probe.get(key), bool):
                safe_probe[key] = probe[key]
        for key in ("width", "height"):
            if isinstance(probe.get(key), int) and not isinstance(probe[key], bool) and 0 < probe[key] <= 16384:
                safe_probe[key] = probe[key]
        safe["final_video_probe"] = safe_probe

    for key in ("final_video_sha256", "generation_plan_sha256"):
        if isinstance(summary.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", summary[key]):
            safe[key] = summary[key]
    if isinstance(summary.get("output_profile"), str) and summary["output_profile"] in {"landscape", "vertical"}:
        safe["output_profile"] = summary["output_profile"]
    safe["computed_at"] = datetime.now(timezone.utc).isoformat()
    return safe


def write_package_manifest(root: str | Path, job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write package_manifest.json and return its contents."""
    root = Path(root)
    if job is not None and not isinstance(job, dict):
        raise ValueError("package manifest job must be an object")
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
        (job or {}).get("final_video_duration_seconds"),
    )
    summary = _public_summary(summary, root)
    job_snapshot: dict[str, Any] = {}
    safe_identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
    for key in ("id", "run_id"):
        value = (job or {}).get(key)
        if isinstance(value, str) and safe_identifier.fullmatch(value):
            job_snapshot[key] = value
    if isinstance((job or {}).get("status"), str) and (job or {}).get("status") in {"queued", "running", "completed", "failed", "cancelled", "editor_package"}:
        job_snapshot["status"] = (job or {})["status"]
    if isinstance((job or {}).get("stage"), str) and (job or {}).get("stage") in {"research", "script", "visuals", "production", "video_generation", "editing", "editor_export", "completed", "failed"}:
        job_snapshot["stage"] = (job or {})["stage"]
    progress = _public_finite_number((job or {}).get("progress"), 0.0, 1.0)
    if progress is not None:
        job_snapshot["progress"] = progress
    for key in ("created_at", "updated_at", "completed_at", "cancelled_at"):
        value = (job or {}).get(key)
        if isinstance(value, str) and len(value) <= 64 and "//" not in value and "http" not in value.lower():
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            job_snapshot[key] = value
    for key in ("duration_seconds", "final_video_duration_seconds"):
        value = _public_finite_number((job or {}).get(key), 0.0, 86400.0)
        if value is not None:
            job_snapshot[key] = value
    if isinstance((job or {}).get("format"), str) and (job or {}).get("format") in {"short", "long"}:
        job_snapshot["format"] = (job or {})["format"]
    if isinstance((job or {}).get("scenes"), int) and not isinstance((job or {}).get("scenes"), bool) and 0 <= (job or {})["scenes"] <= 10000:
        job_snapshot["scenes"] = (job or {})["scenes"]
    if isinstance((job or {}).get("output_profile"), str) and (job or {}).get("output_profile") in {"landscape", "vertical"}:
        job_snapshot["output_profile"] = (job or {})["output_profile"]
    if isinstance((job or {}).get("aspect_ratio"), str) and (job or {}).get("aspect_ratio") in {"16:9", "9:16"}:
        job_snapshot["aspect_ratio"] = (job or {})["aspect_ratio"]
    for key in ("final_video_sha256", "final_video_plan_sha256", "source_digest"):
        value = (job or {}).get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            job_snapshot[key] = value
    attempt = (job or {}).get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool) and 0 <= attempt <= 10000:
        job_snapshot["attempt"] = attempt
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
