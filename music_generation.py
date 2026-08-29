"""Verified Higgsfield Seed Audio generation for the provider canary.

The adapter is intentionally opt-in and stores no provider response URLs. It
publishes only an audio artifact that passed descriptor-bound media verification.
"""
from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from engines.generation_agent import (
    _bounded_int_env,
    _open_provider_url,
    _provider_status_is_usable,
    _provider_url,
)
from media_assembly import MediaError, probe_media
from package_utils import _cleanup_identity, _contain_entry_at, _parse_strict_json
from package_utils import _fsync_verified_publication, _open_directory_no_follow, _publication_lock, _remove_entry_at, _run_bounded_subprocess, _set_response_timeout


class MusicGenerationError(RuntimeError):
    """Raised when provider music generation cannot be verified safely."""


def _cleanup(directory_fd: int, name: str, descriptor: int, *, deadline: float | None = None) -> None:
    if directory_fd < 0 or not name:
        return
    try:
        expired_before_cleanup = deadline is not None and time.monotonic() >= deadline
        if expired_before_cleanup and descriptor < 0:
            return
        if descriptor < 0:
            raise MusicGenerationError("provider audio temporary cleanup identity was not pinned")
        current = os.fstat(descriptor)
        _remove_entry_at(directory_fd, name, _cleanup_identity(current), deadline=deadline)
        if expired_before_cleanup or (deadline is not None and time.monotonic() >= deadline):
            return
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise MusicGenerationError("provider audio temporary cleanup failed") from exc


def _download_verified_audio(
    url: str,
    destination: Path,
    *,
    max_duration_seconds: float | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    max_bytes = _bounded_int_env(
        "SOLO_STUDIO_MAX_AUDIO_BYTES", 100 * 1024 * 1024, 1024, 2 * 1024 * 1024 * 1024
    )
    try:
        directory_fd = _open_directory_no_follow(destination.parent, create=False)
    except OSError as exc:
        raise MusicGenerationError("Higgsfield audio destination is unavailable") from exc
    temporary_name = f".{destination.name}.canary-{os.getpid()}-{os.urandom(8).hex()}"
    descriptor = -1
    published = False
    published_cleanup_identity: tuple[int, int, int] | None = None
    deadline = deadline if deadline is not None else time.monotonic() + _bounded_int_env("SOLO_STUDIO_PROVIDER_DOWNLOAD_TIMEOUT", 300, 1, 3600)
    verified_inode: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MusicGenerationError("provider audio download deadline exceeded")
        response = _open_provider_url(url, min(120.0, remaining), deadline=deadline)
        total = 0
        with response:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MusicGenerationError("provider audio download deadline exceeded")
                _set_response_timeout(response, remaining)
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise MusicGenerationError("provider audio artifact exceeds the configured size limit")
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise MusicGenerationError("provider audio artifact write made no progress")
                    view = view[written:]
                if time.monotonic() >= deadline:
                    raise MusicGenerationError("provider audio download deadline exceeded")
        if time.monotonic() >= deadline:
            raise MusicGenerationError("provider audio download deadline exceeded")
        if total <= 0:
            raise MusicGenerationError("provider returned an empty audio artifact")
        if time.monotonic() >= deadline:
            raise MusicGenerationError("provider audio download deadline exceeded")
        os.fsync(descriptor)
        if time.monotonic() >= deadline:
            raise MusicGenerationError("provider audio download deadline exceeded")
        metadata = probe_media(Path(f"/proc/self/fd/{descriptor}"), _descriptor=descriptor, deadline=deadline)
        if not metadata.get("has_audio") or metadata.get("has_video"):
            raise MusicGenerationError("provider artifact must contain audio only")
        if not math.isfinite(float(metadata.get("duration_seconds", 0))) or float(metadata["duration_seconds"]) <= 0:
            raise MusicGenerationError("provider audio duration is not positive")
        if max_duration_seconds is not None and float(metadata["duration_seconds"]) > max_duration_seconds:
            raise MusicGenerationError("provider audio duration exceeds the canary bound")
        if time.monotonic() >= deadline:
            raise MusicGenerationError("provider audio download deadline exceeded")
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise MusicGenerationError("provider audio artifact is not a regular file")
        verified_inode = (file_stat.st_dev, file_stat.st_ino)
        with _publication_lock(directory_fd, destination.name, deadline=deadline):
            os.link(
                f"/proc/self/fd/{descriptor}",
                destination.name,
                dst_dir_fd=directory_fd,
                follow_symlinks=True,
            )
            published = True
            published_cleanup_identity = _cleanup_identity(os.lstat(destination.name, dir_fd=directory_fd))
            _remove_entry_at(directory_fd, temporary_name, _cleanup_identity(os.fstat(descriptor)), deadline=deadline)
            _fsync_verified_publication(directory_fd, destination.name, verified_inode, deadline=deadline)
            if time.monotonic() >= deadline:
                raise MusicGenerationError("provider audio download deadline exceeded")
        return {
            "status": "downloaded",
            "provider": "higgsfield",
            "bytes": file_stat.st_size,
            "duration_seconds": float(metadata["duration_seconds"]),
            "audio_verified": True,
            "artifact_identity": verified_inode,
            "artifact_sha256": metadata["sha256"],
        }
    except (OSError, ValueError, MediaError, MusicGenerationError) as exc:
        if published and published_cleanup_identity is not None:
            try:
                with _publication_lock(directory_fd, destination.name, deadline=deadline):
                    _contain_entry_at(
                        directory_fd,
                        destination.name,
                        published_cleanup_identity,
                        "provider-audio",
                        deadline=deadline,
                    )
            except (FileNotFoundError, OSError, ValueError) as cleanup_exc:
                raise MusicGenerationError("Higgsfield audio publication cleanup was not proven") from cleanup_exc
        raise MusicGenerationError("Higgsfield audio artifact acquisition failed") from exc
    finally:
        cleanup_error: Exception | None = None
        try:
            _cleanup(directory_fd, temporary_name, descriptor, deadline=deadline)
        except Exception as exc:
            cleanup_error = exc
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = cleanup_error or MusicGenerationError("Higgsfield audio descriptor cleanup failed")
        try:
            os.close(directory_fd)
        except OSError as exc:
            cleanup_error = cleanup_error or MusicGenerationError("Higgsfield audio directory cleanup failed")
        if cleanup_error is not None:
            if isinstance(cleanup_error, MusicGenerationError):
                raise cleanup_error
            raise MusicGenerationError("Higgsfield audio cleanup failed") from cleanup_error


def generate_music(prompt: str, duration: float, destination: str | Path, *, deadline: float | None = None) -> dict[str, Any]:
    """Generate one short instrumental track through Higgsfield Seed Audio."""
    if os.environ.get("SOLO_STUDIO_ENABLE_HIGGSFIELD", "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise MusicGenerationError("Higgsfield audio generation is disabled")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000 or any(
        ord(character) < 32 or ord(character) == 127 for character in prompt
    ):
        raise MusicGenerationError("music prompt is empty")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise MusicGenerationError("music duration must be finite and positive")
    try:
        numeric_duration = float(duration)
    except (OverflowError, TypeError, ValueError) as exc:
        raise MusicGenerationError("music duration must be finite and positive") from exc
    if not math.isfinite(numeric_duration) or numeric_duration <= 0 or numeric_duration > 30:
        raise MusicGenerationError("music duration must be finite and positive")
    timeout = _bounded_int_env("SOLO_STUDIO_HIGGSFIELD_TIMEOUT", 900, 1, 3600)
    download_timeout = _bounded_int_env("SOLO_STUDIO_PROVIDER_DOWNLOAD_TIMEOUT", 300, 1, 3600)
    operation_timeout = _bounded_int_env(
        "SOLO_STUDIO_PROVIDER_OPERATION_TIMEOUT",
        min(7200, timeout + download_timeout),
        1,
        7200,
    )
    operation_deadline = deadline if deadline is not None else time.monotonic() + operation_timeout
    if time.monotonic() >= operation_deadline:
        raise MusicGenerationError("Higgsfield audio operation deadline exceeded")
    command = [
        "higgsfield",
        "generate",
        "create",
        "seed_audio",
        "--prompt",
        prompt[:8000],
        "--wait",
        "--json",
    ]
    try:
        remaining = min(timeout, operation_deadline - time.monotonic())
        if remaining <= 0:
            raise MusicGenerationError("Higgsfield audio operation deadline exceeded")
        process = _run_bounded_subprocess(command, timeout=remaining, deadline=operation_deadline)
    except subprocess.TimeoutExpired as exc:
        raise MusicGenerationError("Higgsfield audio CLI timed out") from exc
    except subprocess.SubprocessError as exc:
        raise MusicGenerationError("Higgsfield audio CLI supervision failed") from exc
    except OSError as exc:
        raise MusicGenerationError("Higgsfield audio CLI could not start") from exc
    if time.monotonic() >= operation_deadline:
        raise MusicGenerationError("Higgsfield audio operation deadline exceeded")
    if process.returncode != 0:
        raise MusicGenerationError("Higgsfield audio CLI returned a non-zero exit")
    try:
        payload = _parse_strict_json(process.stdout)
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise MusicGenerationError("Higgsfield audio CLI returned non-JSON or invalid JSON output")
    try:
        usable = _provider_status_is_usable(payload)
    except RecursionError as exc:
        raise MusicGenerationError("Higgsfield audio provider envelope is too deeply nested") from exc
    if not usable:
        raise MusicGenerationError("Higgsfield audio provider did not report a completed generation")
    try:
        result_url = _provider_url(payload)
    except RecursionError as exc:
        raise MusicGenerationError("Higgsfield audio provider envelope is too deeply nested") from exc
    if not result_url:
        raise MusicGenerationError("Higgsfield audio provider returned no HTTPS result URL")
    attempts = _bounded_int_env("SOLO_STUDIO_PROVIDER_RETRY_ATTEMPTS", 3, 1, 5)
    download_deadline = operation_deadline
    last_error: MusicGenerationError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _download_verified_audio(
                result_url,
                Path(destination),
                max_duration_seconds=max(30.0, min(3600.0, numeric_duration * 4.0)),
                deadline=download_deadline,
            )
        except MusicGenerationError as exc:
            last_error = exc
            if attempt < attempts:
                remaining = download_deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1)), remaining))
    raise last_error or MusicGenerationError("Higgsfield audio generation failed")
