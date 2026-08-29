"""Generate verified video clips through the configured Higgsfield CLI.

Default behavior remains a safe dry-run. Real generation requires:
- ``higgsfield`` in PATH and authenticated;
- ``SOLO_STUDIO_ENABLE_HIGGSFIELD=1``;
- a provider response containing an HTTPS result URL for every scene.

A real run writes downloaded MP4s under ``clips/`` and returns non-zero when any
scene fails, so the worker cannot mark an editor-only package as fully successful.
"""
from __future__ import annotations

import hashlib
import json
import http.client
import fcntl
import ipaddress
import math
import multiprocessing
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_assembly import MEDIA_HASH_MAX_BYTES, MediaError, _sha256, _sha256_descriptor
from package_utils import (_open_directory_no_follow, _open_private_staging_directory, _remove_entry_at, _remove_tree_at,
                           _run_bounded_subprocess, atomic_write_json, normalize_output_profile,
                           read_json_artifact, remove_matching_files, validate_output_profile_contract, _set_response_timeout,
                           _fsync_verified_publication, _parse_strict_json, _publication_lock, _contain_entry_at,
                           _cleanup_identity, _directory_cleanup_identity, _entry_cleanup_identity_at, _rename_exchange)

DEFAULT_MODEL = os.environ.get("SOLO_STUDIO_HIGGSFIELD_MODEL", "seedance_2_0")
TRUTHY = {"1", "true", "yes", "on"}
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _redact_persisted_text(value: object) -> str:
    """Remove provider URLs and secret-like values before durable publication."""
    text = value if isinstance(value, str) else ""
    text = re.sub(r"(?i)https?://\S+", "[provider-url-redacted]", text)
    secret_key = r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|token|secret|password)"
    text = re.sub(
        r"(?i)\b(" + secret_key + r")\b\s*[:=]\s*(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;]+)",
        r"\1=[redacted]",
        text,
    )
    return text[:4096]
DURATION_TOLERANCE_SECONDS = 0.5


class ClipPublicationCleanupPending(OSError):
    """The new clips are published but the old directory still needs cleanup."""

    def __init__(self, backup_name: str, cause: OSError) -> None:
        super().__init__(f"published clips cleanup is pending for {backup_name}")
        self.backup_name = backup_name
        self.__cause__ = cause


def _unlink_if_unchanged(
    directory_fd: int,
    name: str,
    expected_inode: tuple[object, ...],
    *,
    verify_preclaim_ctime: bool = True,
    deadline: float | None = None,
) -> None:
    """Claim and remove only the expected directory entry."""
    _remove_entry_at(
        directory_fd,
        name,
        expected_inode,
        deadline=deadline,
        verify_preclaim_ctime=verify_preclaim_ctime,
    )


_CANONICAL_STAGED_NAME = re.compile(r"(?:generation_plan\.json|scene_[0-9]{2}\.mp4)")


def _validate_staged_name(name: str) -> None:
    if not _CANONICAL_STAGED_NAME.fullmatch(name):
        raise OSError("staged clips contains a non-canonical filename")


def _publish_staged_clips_descriptor(
    staged_dir: Path,
    clips_dir: Path,
    staged_fd: int,
    *,
    deadline: float | None = None,
    expected_hashes: dict[str, str] | None = None,
) -> None:
    """Publish a fully validated staged set through held directory descriptors."""
    if staged_fd < 0 or staged_dir.parent != clips_dir.parent:
        raise OSError("staged clips descriptor or parent is invalid")
    if not isinstance(expected_hashes, dict) or not expected_hashes:
        raise OSError("staged clips publication requires a non-empty expected hash set")
    for expected_name in expected_hashes:
        if not isinstance(expected_name, str):
            raise OSError("staged clips expected names must be strings")
        _validate_staged_name(expected_name)

    def check_deadline() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("staged clips publication deadline exceeded")

    parent_fd = _open_directory_no_follow(clips_dir.parent, create=False)
    clips_fd = -1
    publication_fd = -1
    publication_name = ""
    publication_inode: tuple[int, int] | None = None
    published_canonical = False
    prepared: list[tuple[str, int, tuple[int, int]]] = []
    published_descriptors: list[tuple[str, int, str, tuple[int, int]]] = []
    publication_lock = _publication_lock(parent_fd, clips_dir.name, deadline=deadline)
    lock_acquired = False
    try:
        publication_lock.__enter__()
        lock_acquired = True
        check_deadline()
        staged_inode = os.fstat(staged_fd)
        staged_path_stat = os.lstat(staged_dir.name, dir_fd=parent_fd)
        if (staged_inode.st_dev, staged_inode.st_ino) != (staged_path_stat.st_dev, staged_path_stat.st_ino):
            raise OSError("staged clips source was replaced")
        clips_fd = os.open(
            clips_dir.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        clips_stat = os.fstat(clips_fd)

        # Validate and pin every staged entry before touching the canonical set.
        with os.scandir(staged_fd) as entries:
            for entry in entries:
                check_deadline()
                _validate_staged_name(entry.name)
                source_stat = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(source_stat.st_mode):
                    raise OSError("staged clips contains a non-regular entry")
                source_fd = -1
                try:
                    source_fd = os.open(
                        entry.name,
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=staged_fd,
                    )
                    opened_stat = os.fstat(source_fd)
                    if (opened_stat.st_dev, opened_stat.st_ino) != (source_stat.st_dev, source_stat.st_ino):
                        raise OSError("staged clip changed before publication")
                    if opened_stat.st_size < 0 or opened_stat.st_size > MEDIA_HASH_MAX_BYTES:
                        raise OSError("staged clip exceeds hashing limit")
                    expected_hash = expected_hashes.get(entry.name)
                    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                        raise OSError("staged clip has no valid expected content hash")
                    digest = hashlib.sha256()
                    hashed_bytes = 0
                    os.lseek(source_fd, 0, os.SEEK_SET)
                    for chunk in iter(lambda: os.read(source_fd, 1024 * 1024), b""):
                        check_deadline()
                        hashed_bytes += len(chunk)
                        if hashed_bytes > MEDIA_HASH_MAX_BYTES:
                            raise OSError("staged clip exceeds hashing limit")
                        digest.update(chunk)
                    final_stat = os.fstat(source_fd)
                    if (final_stat.st_dev, final_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino) or final_stat.st_size != opened_stat.st_size:
                        raise OSError("staged clip changed during hashing")
                    if digest.hexdigest() != expected_hash:
                        raise OSError("staged clip content changed before publication")
                    os.lseek(source_fd, 0, os.SEEK_SET)
                except BaseException:
                    if source_fd >= 0:
                        os.close(source_fd)
                    raise
                prepared.append((entry.name, source_fd, (source_stat.st_dev, source_stat.st_ino)))
        if {name for name, _, _ in prepared} != set(expected_hashes):
            raise OSError("staged clips do not match the expected generation set")

        publication_name, publication_fd, publication_inode = cast(
            tuple[str, int, tuple[int, int]],
            _open_private_staging_directory(parent_fd, prefix=".clips.publish-", include_identity=True),
        )
        # Materialize into the private publication directory. The canonical
        # directory remains untouched until every entry is verified.
        try:
            for name, source_fd, source_inode in prepared:
                check_deadline()
                expected_hash = expected_hashes[name]
                destination_fd = -1
                try:
                    # Do not hard-link the staged source: a same-UID writer can
                    # mutate that inode after validation and thereby mutate the
                    # published clip. Materialize a distinct destination inode
                    # from the held, validated descriptor and verify that inode
                    # before it becomes part of the canonical set.
                    destination_fd = os.open(
                        name,
                        os.O_RDWR
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=publication_fd,
                    )
                    os.lseek(source_fd, 0, os.SEEK_SET)
                    copied = 0
                    for chunk in iter(lambda: os.read(source_fd, 1024 * 1024), b""):
                        check_deadline()
                        copied += len(chunk)
                        if copied > MEDIA_HASH_MAX_BYTES:
                            raise OSError("staged clip exceeds publication limit")
                        view = memoryview(chunk)
                        while view:
                            written = os.write(destination_fd, view)
                            if written <= 0:
                                raise OSError("staged clip publication made no progress")
                            view = view[written:]
                    os.fsync(destination_fd)
                    published_stat = os.fstat(destination_fd)
                    if not stat.S_ISREG(published_stat.st_mode) or published_stat.st_size != copied:
                        raise OSError("published staged clip has invalid size")
                    published_hash = _sha256_descriptor(
                        destination_fd,
                        deadline=deadline,
                        max_bytes=MEDIA_HASH_MAX_BYTES,
                    )
                    if published_hash != expected_hash:
                        raise OSError("published staged clip content changed")
                    linked = os.lstat(name, dir_fd=publication_fd)
                    if (linked.st_dev, linked.st_ino) != (published_stat.st_dev, published_stat.st_ino):
                        raise OSError("published staged clip inode changed")
                    published_descriptors.append((name, destination_fd, expected_hash, (published_stat.st_dev, published_stat.st_ino)))
                    destination_fd = -1
                finally:
                    if destination_fd >= 0:
                        os.close(destination_fd)
            check_deadline()
            os.fsync(publication_fd)
            check_deadline()
            old_clips_identity = _directory_cleanup_identity(clips_fd)
            _rename_exchange(parent_fd, clips_dir.name, parent_fd, publication_name)
            published_canonical = True
            canonical_identity = _directory_cleanup_identity(publication_fd)
            current_clips_fd = os.open(clips_dir.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                current_clips_identity = _directory_cleanup_identity(current_clips_fd)
            finally:
                os.close(current_clips_fd)
            if current_clips_identity != canonical_identity:
                _contain_entry_at(
                    parent_fd,
                    clips_dir.name,
                    canonical_identity,
                    "clips-publication",
                    deadline=deadline,
                )
                raise OSError("canonical clips directory was replaced during publication")
            os.fsync(parent_fd)
            check_deadline()
            for name, descriptor, expected_hash, expected_inode in published_descriptors:
                check_deadline()
                if _sha256_descriptor(descriptor, deadline=deadline, max_bytes=MEDIA_HASH_MAX_BYTES) != expected_hash:
                    raise OSError("published staged clip changed after materialization")
                current_stat = os.fstat(descriptor)
                current_entry = os.lstat(name, dir_fd=publication_fd)
                if (current_stat.st_dev, current_stat.st_ino) != expected_inode or (current_entry.st_dev, current_entry.st_ino) != expected_inode:
                    raise OSError("published staged clip inode changed after materialization")
            check_deadline()
            canonical_identity = _directory_cleanup_identity(publication_fd)
            final_clips_fd = os.open(clips_dir.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                final_clips_identity = _directory_cleanup_identity(final_clips_fd)
            finally:
                os.close(final_clips_fd)
            if final_clips_identity != canonical_identity:
                _contain_entry_at(
                    parent_fd,
                    clips_dir.name,
                    canonical_identity,
                    "clips-publication",
                    deadline=deadline,
                )
                raise OSError("canonical clips directory was replaced after publication durability")
            check_deadline()
            assert publication_inode is not None
            try:
                _remove_tree_at(
                    parent_fd,
                    publication_name,
                    old_clips_identity,
                    deadline=deadline,
                )
            except OSError as cleanup_exc:
                raise ClipPublicationCleanupPending(publication_name, cleanup_exc) from cleanup_exc
            for name, descriptor, expected_hash, expected_inode in published_descriptors:
                check_deadline()
                current = os.stat(name, dir_fd=publication_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != expected_inode:
                    raise OSError("canonical staged clip inode changed during old-directory cleanup")
                if _sha256_descriptor(descriptor, deadline=deadline, max_bytes=MEDIA_HASH_MAX_BYTES) != expected_hash:
                    raise OSError("canonical staged clip changed during old-directory cleanup")
            publication_name = ""
        except BaseException:
            if published_canonical:
                raise
            if publication_fd >= 0:
                with os.scandir(publication_fd) as entries:
                    for entry in entries:
                        check_deadline()
                        existing_identity = _entry_cleanup_identity_at(publication_fd, entry.name)
                        _remove_entry_at(publication_fd, entry.name, existing_identity, deadline=deadline)
                check_deadline()
                os.fsync(publication_fd)
            raise
    finally:
        cleanup_error: Exception | None = None
        for _, descriptor, _, _ in published_descriptors:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        for _, source_fd, _ in prepared:
            try:
                os.close(source_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if publication_fd >= 0:
            try:
                os.close(publication_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if publication_name and not published_canonical and publication_inode is not None:
            try:
                _remove_entry_at(
                    parent_fd,
                    publication_name,
                    (publication_inode[0], publication_inode[1], "held-directory"),
                    deadline=deadline,
                )
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if clips_fd >= 0:
            try:
                os.close(clips_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if lock_acquired:
            try:
                publication_lock.__exit__(None, None, None)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        try:
            os.close(parent_fd)
        except OSError as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise OSError("staged clips publication cleanup was not proven") from cleanup_error




def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, value))


def _cleanup_temporary(directory_fd: int, name: str, descriptor: int = -1, deadline: float | None = None) -> None:
    if not name or directory_fd < 0:
        return
    opened = -1
    try:
        expired = deadline is not None and time.monotonic() >= deadline
        if descriptor < 0:
            return
        current = os.fstat(descriptor)
        _unlink_if_unchanged(directory_fd, name, _cleanup_identity(current), deadline=deadline)
        if not expired and (deadline is None or time.monotonic() < deadline):
            os.fsync(directory_fd)
    except FileNotFoundError:
        pass
    finally:
        if opened >= 0:
            os.close(opened)


MAX_CLIP_BYTES = _bounded_int_env("SOLO_STUDIO_MAX_CLIP_BYTES", 200 * 1024 * 1024, 1, 2 * 1024 * 1024 * 1024)
_PROVIDER_DNS_SLOT = threading.BoundedSemaphore(1)


def higgsfield_enabled() -> bool:
    """Read the enable flag at runtime so tests/workers can safely override env."""
    return os.environ.get("SOLO_STUDIO_ENABLE_HIGGSFIELD", "").strip().lower() in TRUTHY


def load_json(path: str | Path) -> dict[str, Any]:
    return read_json_artifact(path)


def sanitize_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:8000]


def seedance_prompt(scene: dict[str, Any], output_profile: dict[str, Any] | None = None) -> str:
    profile = output_profile if output_profile is not None else validate_output_profile_contract(scene, "scene")
    duration = max(float(scene.get("duration_seconds", 5) or 5), 1.0)
    visual = (
        scene.get("visual_description")
        or scene.get("kling_prompt")
        or scene.get("runway_prompt")
        or scene.get("pika_prompt")
        or scene.get("seedance_prompt")
        or ""
    )
    camera = scene.get("camera") or "cinematic medium shot with controlled motion"
    style = scene.get("visual_style") or "cinematic, high-quality, coherent lighting"
    transition = scene.get("transition") or "cut"
    rules = scene.get("rules") or "keep subject identity and scene continuity stable; no random props; no text unless explicitly requested"
    narration = scene.get("narration") or visual
    b1 = round(duration * 0.20, 1)
    b2 = round(duration * 0.47, 1)
    b3 = round(duration * 0.80, 1)
    beats = [
        (0, b1, "set the scene"),
        (b1, b2, "build the action"),
        (b2, b3, "the turn / strongest visual moment"),
        (b3, round(duration, 1), "resolution / ending frame"),
    ]
    beat_text = "\n".join(
        f"- {start:g}-{end:g}s: {label}. What: {visual}. Action: {narration}. "
        f"Camera: {camera}. Style: {style}. Rules: {rules}."
        for start, end, label in beats
    )
    return sanitize_prompt(
        f"Generate a {duration:g}-second {profile['aspect_ratio']} {profile['output_profile']} video clip for scene {scene.get('scene_number', '?')}.\n"
        f"Use this four-beat structure:\n{beat_text}\n"
        f"Transition intent: {transition}. Audio: natural synchronized ambience unless a separate voiceover/music track is provided."
    )


def _provider_url(payload: Any) -> str | None:
    if isinstance(payload, list):
        return next(
            (url for item in payload if (url := _provider_url(item))),
            None,
        )
    if not isinstance(payload, dict):
        return None

    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.extend(payload.get(key) for key in ("result_url", "url", "video_url", "audio_url", "file_url", "download_url"))
        for key in ("result", "output", "assets", "data", "job"):
            nested = payload.get(key)
            if isinstance(nested, (dict, list)):
                candidates.append(_provider_url(nested))
    return next((value for value in candidates if isinstance(value, str) and value.startswith("https://")), None)


def _provider_status_is_usable(payload: Any) -> bool:
    accepted = {"completed", "succeeded", "success", "ready", "done", "downloaded", "finished"}
    rejected = {"failed", "failure", "error", "errored", "cancelled", "canceled", "pending", "processing", "submitted", "queued", "unknown", "expired"}
    if isinstance(payload, dict):
        for key in ("error", "errors", "failure", "failed"):
            if key in payload and payload[key] not in (None, "", False, [], {}):
                return False
        for key in ("status", "state"):
            if key in payload:
                status = str(payload[key]).strip().lower()
                if status in rejected or status not in accepted:
                    return False
        return all(_provider_status_is_usable(value) for value in payload.values())
    if isinstance(payload, list):
        return all(_provider_status_is_usable(value) for value in payload)
    return True


def _verify_clip(
    path: Path,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
    expected_duration: float | None = None,
    deadline: float | None = None,
    _descriptor: int | None = None,
) -> tuple[bool, str | None]:
    """Require an MP4 signature, positive duration, and profile dimensions."""
    descriptor_owned = _descriptor is None
    try:
        if descriptor_owned and (not path.is_absolute() or path.resolve(strict=True) != path):
            return False, "Provider returned an artifact outside the canonical clip path."
        descriptor = _descriptor if _descriptor is not None else os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except (OSError, ValueError):
        return False, "Provider returned an unavailable MP4 artifact."
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size < 12:
            return False, "Provider returned an invalid or empty MP4 file."
        os.lseek(descriptor, 0, os.SEEK_SET)
        header = os.read(descriptor, 12)
        if header[4:8] != b"ftyp":
            return False, "Provider returned a non-MP4 artifact."
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return False, "ffprobe is required to verify generated clips."
        probe_timeout = 30.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "Generated clip verification deadline exceeded."
            probe_timeout = min(probe_timeout, remaining)
        try:
            probe = _run_bounded_subprocess(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_type,width,height:format=duration", "-of", "json",
                 f"/proc/self/fd/{descriptor}"],
                timeout=probe_timeout,
                pass_fds=(descriptor,),
                deadline=deadline,
            )
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            return False, "ffprobe could not verify the generated clip."
        if probe.returncode != 0:
            return False, "ffprobe rejected the generated clip."
        if deadline is not None and time.monotonic() >= deadline:
            return False, "Generated clip verification deadline exceeded."
        try:
            probe_payload = _parse_strict_json(probe.stdout)
            streams = probe_payload.get("streams", [])
            video_stream = next(
                (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"),
                None,
            )
            if video_stream is None:
                return False, "Generated clip contains no video stream."
            duration = float(probe_payload.get("format", {}).get("duration"))
            width = height = None
            if expected_width is not None or expected_height is not None:
                raw_width = video_stream.get("width")
                raw_height = video_stream.get("height")
                if (
                    isinstance(raw_width, bool)
                    or not isinstance(raw_width, (int, str))
                    or isinstance(raw_height, bool)
                    or not isinstance(raw_height, (int, str))
                ):
                    raise ValueError("invalid video dimensions")
                width = int(raw_width)
                height = int(raw_height)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return False, "ffprobe returned no usable clip duration."
        if not math.isfinite(duration) or duration <= 0:
            return False, "Generated clip duration must be positive."
        if expected_duration is not None:
            if (
                isinstance(expected_duration, bool)
                or not isinstance(expected_duration, (int, float))
                or not math.isfinite(float(expected_duration))
                or float(expected_duration) <= 0
                or abs(duration - float(expected_duration)) > DURATION_TOLERANCE_SECONDS
            ):
                return False, "Generated clip duration does not match the requested scene duration."
        if expected_width is not None and width != expected_width:
            return False, "Generated clip width does not match the selected profile."
        if expected_height is not None and height != expected_height:
            return False, "Generated clip height does not match the selected profile."
        if deadline is not None and time.monotonic() >= deadline:
            return False, "Generated clip verification deadline exceeded."
        return True, None
    finally:
        if descriptor_owned:
            os.close(descriptor)


def _resolve_addresses_child(host: str, port: int, connection: Any) -> None:
    try:
        connection.send(("ok", socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except BaseException:
        try:
            connection.send(("error",))
        except (BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


def _reap_provider_dns_worker(process: Any, *, deadline: float | None = None, force: bool = False) -> bool:
    reap_grace = 0.01

    def join_with_budget(*, after_force_kill: bool = False) -> None:
        remaining = 0.0 if deadline is None else max(0.0, deadline - time.monotonic())
        if after_force_kill:
            remaining = max(remaining, reap_grace)
        process.join(remaining)

    if process.is_alive():
        process.terminate()
        join_with_budget()
    if process.is_alive() and force:
        process.kill()
        join_with_budget(after_force_kill=True)
    return not process.is_alive()


def _resolve_provider_addresses(host: str, port: int, deadline: float | None = None) -> set[str]:
    """Resolve a provider host in a killable child bounded by the operation budget."""
    remaining = (deadline - time.monotonic()) if deadline is not None else 120.0
    if remaining <= 0:
        raise TimeoutError("Provider DNS resolution deadline exceeded")
    if "fork" not in multiprocessing.get_all_start_methods():
        raise OSError("Provider DNS supervision unavailable")
    if not _PROVIDER_DNS_SLOT.acquire(blocking=False):
        raise TimeoutError("Provider DNS resolution capacity exhausted")
    parent = child = resolver = None
    resolver_started = False
    try:
        context = multiprocessing.get_context("fork")
        parent, child = context.Pipe(duplex=False)
        resolver = context.Process(target=_resolve_addresses_child, args=(host, port, child))
        resolver.start()
        resolver_started = True
    except BaseException:
        if resolver is not None and resolver_started:
            try:
                _reap_provider_dns_worker(resolver, deadline=deadline, force=True)
            except BaseException:
                pass
        for connection in (parent, child):
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
        _PROVIDER_DNS_SLOT.release()
        raise
    assert parent is not None and child is not None and resolver is not None
    try:
        child.close()
        child = None
        remaining = 120.0 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Provider DNS resolution deadline exceeded")
        if not parent.poll(remaining):
            if not _reap_provider_dns_worker(resolver, deadline=deadline, force=True):
                raise OSError("Provider DNS resolver cleanup failed")
            raise TimeoutError("Provider DNS resolution deadline exceeded")
        try:
            payload = parent.recv()
        except (EOFError, OSError):
            payload = None
        if not _reap_provider_dns_worker(resolver, deadline=deadline, force=True):
            raise OSError("Provider DNS resolver cleanup failed")
        if not isinstance(payload, tuple) or not payload or payload[0] != "ok":
            raise OSError("Provider host resolution failed")
        raw_addresses = payload[1]
        if not isinstance(raw_addresses, list):
            raise OSError("Provider host resolution returned invalid data")
        return {sockaddr[4][0] for sockaddr in raw_addresses}
    finally:
        if child is not None:
            child.close()
        parent.close()
        _PROVIDER_DNS_SLOT.release()


def _public_provider_ip(url: str, deadline: float | None = None) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Provider artifact URL must use HTTPS.")
    if parsed.port not in (None, 443):
        raise ValueError("Provider artifact URL must use HTTPS on port 443.")
    try:
        addresses = _resolve_provider_addresses(parsed.hostname, parsed.port or 443, deadline)
    except TimeoutError:
        raise
    except OSError as exc:
        raise ValueError("Provider artifact host could not be resolved.") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Provider artifact host is not publicly routable.")
    return str(sorted(addresses)[0])


def _assert_safe_provider_url(url: str, deadline: float | None = None) -> None:
    parsed = urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Provider artifact URL must not contain credentials.")
    allowed_hosts = {
        value.strip().lower()
        for value in os.environ.get("SOLO_STUDIO_HIGGSFIELD_ALLOWED_HOSTS", "").split(",")
        if value.strip()
    }
    if not parsed.hostname or parsed.hostname.lower() not in allowed_hosts:
        raise ValueError("Provider artifact host is not explicitly allowlisted.")
    _public_provider_ip(url, deadline)


class _SafeProviderRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, deadline: float | None = None):
        super().__init__()
        self.deadline = deadline

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_safe_provider_url(newurl, self.deadline)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, *, pinned_ip: str, deadline: float | None = None, **kwargs):
        super().__init__(host, **kwargs)
        self.pinned_ip = pinned_ip
        self.deadline = deadline

    def connect(self):
        source_address = getattr(self, "source_address", None)
        remaining = self.timeout if self.deadline is None else self.deadline - time.monotonic()
        if remaining is None:
            remaining = 120.0
        if remaining <= 0:
            raise TimeoutError("Provider connection deadline exceeded")
        self.sock = socket.create_connection((self.pinned_ip, self.port), remaining, source_address)
        tunnel_host = getattr(self, "_tunnel_host", None)
        if tunnel_host:
            remaining = self.timeout if self.deadline is None else self.deadline - time.monotonic()
            if remaining is None:
                remaining = 120.0
            if remaining <= 0:
                raise TimeoutError("Provider connection deadline exceeded")
            self.sock.settimeout(remaining)
            getattr(self, "_tunnel")()
        remaining = self.timeout if self.deadline is None else self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Provider TLS deadline exceeded")
        self.sock.settimeout(remaining)
        self.sock = getattr(self, "_context").wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline: float | None = None):
        super().__init__()
        self.deadline = deadline

    def https_open(self, req):
        pinned_ip = _public_provider_ip(req.full_url, self.deadline)
        def connection_factory(host, **kwargs):
            remaining = self.deadline - time.monotonic() if self.deadline is not None else kwargs.get("timeout")
            if remaining is None:
                remaining = 120.0
            if remaining <= 0:
                raise TimeoutError("Provider connection deadline exceeded")
            kwargs["timeout"] = remaining
            return _PinnedHTTPSConnection(host, pinned_ip=pinned_ip, deadline=self.deadline, **kwargs)

        return self.do_open(
            connection_factory,
            req,
        )


def _open_provider_url(url: str, timeout: float = 120, *, deadline: float | None = None):
    timeout = max(0.0, float(timeout))
    if deadline is None:
        deadline = time.monotonic() + timeout
    else:
        timeout = min(timeout, max(0.0, deadline - time.monotonic()))
    if timeout <= 0:
        raise TimeoutError("provider URL opening deadline exceeded")
    _assert_safe_provider_url(url, deadline)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _PinnedHTTPSHandler(deadline),
        _SafeProviderRedirectHandler(deadline),
    )
    return opener.open(url, timeout=timeout)


def _retry_settings() -> tuple[int, float]:
    attempts = _bounded_int_env("SOLO_STUDIO_PROVIDER_RETRY_ATTEMPTS", 3, 1, 5)
    try:
        base_delay = float(os.environ.get("SOLO_STUDIO_PROVIDER_RETRY_BASE_SECONDS", "1"))
        if not math.isfinite(base_delay):
            raise ValueError
        base_delay = max(0.0, min(60.0, base_delay))
    except (TypeError, ValueError, OverflowError):
        base_delay = 1.0
    return attempts, base_delay


def _sleep_before_retry(attempt: int, base_delay: float, remaining: float | None = None) -> None:
    if base_delay <= 0:
        return
    delay = min(60.0, base_delay * (2 ** max(0, attempt - 1)))
    if remaining is not None:
        delay = min(delay, max(0.0, remaining))
    if delay > 0:
        time.sleep(delay)


def _retryable_download_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (TimeoutError, socket.timeout, ConnectionError, OSError))
    return isinstance(exc, (TimeoutError, socket.timeout, ConnectionError))


def _download_provider_artifact(url: str, destination: Path, *, deadline: float | None = None) -> tuple[int, int, int, str]:
    """Download with bounded retry for transport errors only."""
    attempts, base_delay = _retry_settings()
    if deadline is None:
        deadline = time.monotonic() + _bounded_int_env("SOLO_STUDIO_PROVIDER_DOWNLOAD_TIMEOUT", 300, 1, 3600)
    last_error: Exception | None = None
    directory_fd = _open_directory_no_follow(destination.parent, create=False)
    temporary_name = f".{destination.name}.part-{os.getpid()}-{os.urandom(8).hex()}"
    try:
        descriptor = os.open(
            temporary_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
    except Exception:
        os.close(directory_fd)
        raise
    try:
        for attempt in range(1, attempts + 1):
            try:
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)
                total = 0
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("provider artifact download deadline exceeded")
                with os.fdopen(os.dup(descriptor), "wb") as handle, _open_provider_url(url, min(120.0, remaining), deadline=deadline) as response:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("provider artifact download deadline exceeded")
                        _set_response_timeout(response, remaining)
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_CLIP_BYTES:
                            raise ValueError("Provider artifact exceeds the configured size limit.")
                        handle.write(chunk)
                        if time.monotonic() >= deadline:
                            raise TimeoutError("provider artifact download deadline exceeded")
                if time.monotonic() >= deadline:
                    raise TimeoutError("provider artifact download deadline exceeded")
                if total == 0:
                    raise ValueError("Provider returned an empty video file.")
                if time.monotonic() >= deadline:
                    raise TimeoutError("provider artifact download deadline exceeded")
                os.fsync(descriptor)
                if time.monotonic() >= deadline:
                    raise TimeoutError("provider artifact download deadline exceeded")
                # Transfer ownership of the still-open descriptor and its
                # parent directory to the caller. The caller must verify and
                # publish this exact inode before either descriptor is closed.
                result = (total, descriptor, directory_fd, temporary_name)
                descriptor = -1
                directory_fd = -1
                temporary_name = ""
                return result
            except ValueError:
                raise
            except Exception as exc:
                if not _retryable_download_error(exc):
                    raise RuntimeError("provider artifact download failed") from exc
                last_error = exc
                if attempt < attempts:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    _sleep_before_retry(attempt, base_delay, remaining)
        raise RuntimeError("provider artifact download failed") from last_error
    finally:
        cleanup_error: Exception | None = None
        try:
            _cleanup_temporary(directory_fd, temporary_name, descriptor, deadline=deadline)
        except Exception as exc:
            cleanup_error = exc
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise RuntimeError("provider artifact temporary cleanup was not proven") from cleanup_error


def run_fake_provider(
    prompt: str,
    duration: float,
    out_file: Path,
    profile: dict[str, Any],
    deadline: float | None = None,
) -> dict[str, Any]:
    """Generate a deterministic local MP4 for credential-free integration tests."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"status": "failed", "error": "Fake provider requires ffmpeg."}
    width = int(profile["width"])
    height = int(profile["height"])
    operation_deadline = deadline if deadline is not None else time.monotonic() + 120
    if time.monotonic() >= operation_deadline:
        return {"status": "failed", "error": "Fake provider operation deadline exceeded."}
    color = "#" + hashlib.sha256(f"{profile['output_profile']}:{prompt}".encode()).hexdigest()[:6]
    directory_fd = -1
    temporary_fd = -1
    publication_linked = False
    verified_inode = (0, 0)
    published_cleanup_identity: tuple[object, ...] | None = None
    temporary_name = f".{out_file.stem}.fake-{os.getpid()}-{os.urandom(8).hex()}.mp4"
    try:
        directory_fd = _open_directory_no_follow(out_file.parent, create=False)
        temporary_fd = os.open(
            temporary_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        temporary_stat = os.fstat(temporary_fd)
        if not stat.S_ISREG(temporary_stat.st_mode):
            raise OSError("fake-provider temporary output is not a regular file")
        temporary_path = Path(f"/proc/self/fd/{temporary_fd}")
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:r=30:d={duration}",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t", str(duration), "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            "-movflags", "+faststart", "-f", "mp4", str(temporary_path),
        ]
        process = _run_bounded_subprocess(
            command,
            timeout=max(0.001, min(120, operation_deadline - time.monotonic())),
            pass_fds=(directory_fd, temporary_fd),
            deadline=operation_deadline,
        )
        if time.monotonic() >= operation_deadline:
            return {"status": "failed", "error": "Fake provider operation deadline exceeded."}
        if process.returncode != 0:
            return {"status": "failed", "error": "Fake provider ffmpeg generation failed."}
        if time.monotonic() >= operation_deadline:
            return {"status": "failed", "error": "Fake provider operation deadline exceeded."}
        valid, error = _verify_clip(
            Path(f"/proc/self/fd/{temporary_fd}"),
            expected_width=width,
            expected_height=height,
            expected_duration=duration,
            deadline=operation_deadline,
            _descriptor=temporary_fd,
        )
        if not valid:
            return {"status": "failed", "error": error or "Fake provider clip verification failed."}
        verified_stat = os.fstat(temporary_fd)
        verified_inode = (verified_stat.st_dev, verified_stat.st_ino)
        published_cleanup_identity = _cleanup_identity(verified_stat)
        verified_sha256 = _sha256_descriptor(temporary_fd, deadline=operation_deadline)
        with _publication_lock(directory_fd, out_file.name, deadline=operation_deadline):
            os.link(
                f"/proc/self/fd/{temporary_fd}",
                out_file.name,
                dst_dir_fd=directory_fd,
                follow_symlinks=True,
            )
            publication_linked = True
            _unlink_if_unchanged(
                directory_fd,
                temporary_name,
                _cleanup_identity(os.fstat(temporary_fd)),
                deadline=operation_deadline,
            )
            _fsync_verified_publication(directory_fd, out_file.name, verified_inode, deadline=operation_deadline)
        return {
            "status": "downloaded",
            "bytes": verified_stat.st_size,
            "sha256": verified_sha256,
            "duration_verified": True,
            "provider": "fake",
        }
    except (OSError, MediaError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        cleanup_error = None
        if publication_linked and directory_fd >= 0:
            try:
                if published_cleanup_identity is None:
                    raise OSError("published fake-provider artifact identity was not pinned")
                _unlink_if_unchanged(
                    directory_fd,
                    out_file.name,
                    published_cleanup_identity,
                    verify_preclaim_ctime=False,
                    deadline=operation_deadline,
                )
                if time.monotonic() < operation_deadline:
                    os.fsync(directory_fd)
            except FileNotFoundError:
                pass
            except OSError as cleanup_exc:
                cleanup_error = f"published fake-provider artifact cleanup was not proven: {cleanup_exc.__class__.__name__}"
        result = {"status": "failed", "error": "Fake provider could not generate a verified clip."}
        if cleanup_error:
            result["cleanup_error"] = cleanup_error
        return result
    finally:
        cleanup_failure: Exception | None = None
        try:
            _cleanup_temporary(directory_fd, temporary_name, temporary_fd, deadline=operation_deadline)
        except Exception as exc:
            cleanup_failure = exc
        if temporary_fd >= 0:
            try:
                os.close(temporary_fd)
            except OSError as exc:
                cleanup_failure = cleanup_failure or exc
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError as exc:
                cleanup_failure = cleanup_failure or exc
        if cleanup_failure is not None:
            raise RuntimeError("Fake provider temporary artifact cleanup was not proven") from cleanup_failure


def run_higgsfield(
    prompt: str,
    duration: float,
    out_file: Path,
    model: str,
    aspect_ratio: str = "16:9",
    expected_width: int | None = None,
    expected_height: int | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    if os.environ.get("SOLO_STUDIO_ENABLE_HIGGSFIELD", "").strip().lower() not in TRUTHY:
        return {"status": "failed", "error": "Higgsfield generation is disabled."}
    if isinstance(duration, bool) or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000 or any(
        ord(character) < 32 or ord(character) == 127 for character in prompt
    ):
        return {"status": "failed", "error": "Higgsfield prompt or duration input is invalid."}
    try:
        requested_duration = float(duration)
    except (TypeError, ValueError, OverflowError):
        return {"status": "failed", "error": "Higgsfield duration must be finite and positive."}
    if not math.isfinite(requested_duration) or requested_duration < 1.0 or requested_duration > 900:
        return {"status": "failed", "error": "Higgsfield duration is outside the supported bound."}
    if not isinstance(aspect_ratio, str) or aspect_ratio not in {"16:9", "9:16"}:
        return {"status": "failed", "error": "Higgsfield aspect ratio is unsupported."}
    if not isinstance(model, str):
        return {"status": "failed", "error": "Higgsfield model is invalid."}
    model = model.strip()
    if MODEL_PATTERN.fullmatch(model) is None:
        return {"status": "failed", "error": "Higgsfield model is invalid."}
    for label, value in (("width", expected_width), ("height", expected_height)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            return {"status": "failed", "error": f"Higgsfield expected {label} is invalid."}
    resolution = os.environ.get("SOLO_STUDIO_HIGGSFIELD_RESOLUTION", "1080p").strip().lower()
    if resolution not in {"720p", "1080p", "4k"}:
        return {"status": "failed", "error": "Higgsfield resolution is unsupported."}
    duration = requested_duration
    timeout_seconds = _bounded_int_env("SOLO_STUDIO_HIGGSFIELD_TIMEOUT", 900, 1, 3600)
    download_timeout_seconds = _bounded_int_env("SOLO_STUDIO_PROVIDER_DOWNLOAD_TIMEOUT", 300, 1, 3600)
    operation_timeout_seconds = _bounded_int_env(
        "SOLO_STUDIO_PROVIDER_OPERATION_TIMEOUT",
        min(7200, timeout_seconds + download_timeout_seconds),
        1,
        7200,
    )
    operation_deadline = deadline if deadline is not None else time.monotonic() + operation_timeout_seconds
    if time.monotonic() >= operation_deadline:
        return {"status": "failed", "error": "Higgsfield provider operation deadline exceeded."}
    command = [
        "higgsfield", "generate", "create", model,
        "--prompt", prompt,
        "--aspect_ratio", aspect_ratio,
        "--duration", str(int(round(duration))),
        "--resolution", resolution,
        "--wait", "--json",
    ]
    # Submission is intentionally single-shot.  With --wait, a timeout or
    # non-zero exit may still mean the provider accepted remote work; retrying
    # the whole command can create duplicate billable generations.  Only the
    # subsequent artifact download has bounded transport retries.
    try:
        remaining = min(timeout_seconds, operation_deadline - time.monotonic())
        if remaining <= 0:
            return {"status": "failed", "error": "Higgsfield provider operation deadline exceeded."}
        process = _run_bounded_subprocess(
            command,
            timeout=remaining,
            deadline=operation_deadline,
        )
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": "Higgsfield CLI timed out. Check provider logs on the host."}
    except subprocess.SubprocessError:
        return {"status": "failed", "error": "Higgsfield CLI supervision failed. Check provider logs on the host."}
    except OSError as exc:
        return {"status": "failed", "error": f"Higgsfield CLI could not start: {exc.__class__.__name__}"}

    if time.monotonic() >= operation_deadline:
        return {"status": "failed", "error": "Higgsfield provider operation deadline exceeded."}

    # Do not persist raw provider stdout/stderr in the public package manifest.
    # Provider logs can contain signed URLs, credentials, account IDs, or
    # prompts the operator did not intend to redistribute.
    result: dict[str, Any] = {
        "status": "failed" if process.returncode else "submitted",
        "returncode": process.returncode,
    }
    if process.returncode:
        result["error"] = "Higgsfield CLI returned a non-zero exit. Check provider logs on the host."
        return result

    try:
        payload = _parse_strict_json(process.stdout)
    except (json.JSONDecodeError, ValueError, RecursionError):
        result["status"] = "failed"
        result["error"] = "Higgsfield CLI returned non-JSON or invalid JSON output; refusing to infer a video URL."
        return result
    try:
        usable = _provider_status_is_usable(payload)
    except RecursionError:
        result["status"] = "failed"
        result["error"] = "Provider response envelope is too deeply nested."
        return result
    if not usable:
        result["status"] = "failed"
        result["error"] = "Provider did not report a completed generation."
        return result
    try:
        result_url = _provider_url(payload)
    except RecursionError:
        result["status"] = "failed"
        result["error"] = "Provider response envelope is too deeply nested."
        return result
    if not result_url:
        result["status"] = "failed"
        result["error"] = "Provider completed without an HTTPS video URL."
        return result

    temporary_file = out_file.with_name(f".{out_file.name}.part-{os.getpid()}-{os.urandom(8).hex()}")
    download_fd = -1
    download_directory_fd = -1
    temporary_name = ""
    published_bytes = 0
    publication_linked = False
    verified_inode = (0, 0)
    published_cleanup_identity: tuple[object, ...] | None = None
    cleanup_error: Exception | None = None
    try:
        total, download_fd, download_directory_fd, temporary_name = _download_provider_artifact(
            result_url, temporary_file, deadline=operation_deadline
        )
        if total == 0:
            raise ValueError("Provider returned an empty video file.")
        if time.monotonic() >= operation_deadline:
            raise TimeoutError("provider operation deadline exceeded")
        os.lseek(download_fd, 0, os.SEEK_SET)
        temporary_file_bound = Path(f"/proc/self/fd/{download_directory_fd}/{temporary_name}")
        valid, verification_error = _verify_clip(
            temporary_file_bound,
            expected_width=expected_width,
            expected_height=expected_height,
            expected_duration=duration,
            deadline=operation_deadline,
            _descriptor=download_fd,
        )
        if not valid:
            result["status"] = "failed"
            result["error"] = verification_error
            return result
        if time.monotonic() >= operation_deadline:
            raise TimeoutError("provider operation deadline exceeded")
        verified_stat = os.fstat(download_fd)
        verified_inode = (verified_stat.st_dev, verified_stat.st_ino)
        # Publish the inode that was verified, not whatever later appears
        # under the temporary pathname. Existing destinations are refused.
        with _publication_lock(download_directory_fd, out_file.name, deadline=operation_deadline):
            os.link(
                f"/proc/self/fd/{download_fd}",
                out_file.name,
                dst_dir_fd=download_directory_fd,
                follow_symlinks=True,
            )
            publication_linked = True
            published_cleanup_identity = _cleanup_identity(os.fstat(download_fd))
            _unlink_if_unchanged(
                download_directory_fd,
                temporary_name,
                published_cleanup_identity,
                deadline=operation_deadline,
            )
            _fsync_verified_publication(download_directory_fd, out_file.name, verified_inode, deadline=operation_deadline)
            if time.monotonic() >= operation_deadline:
                raise TimeoutError("provider operation deadline exceeded")
            published_sha256 = _sha256_descriptor(download_fd, deadline=operation_deadline)
            final_stat = os.fstat(download_fd)
            final_identity = _cleanup_identity(final_stat)
            published_canonical_stat = os.lstat(out_file.name, dir_fd=download_directory_fd)
            if (
                (final_stat.st_dev, final_stat.st_ino) != verified_inode
                or final_stat.st_size != verified_stat.st_size
                or _cleanup_identity(published_canonical_stat) != final_identity
            ):
                raise OSError("published provider artifact changed after integrity verification")
            published_bytes = final_stat.st_size
    except Exception:
        if publication_linked and download_directory_fd >= 0:
            try:
                with _publication_lock(download_directory_fd, out_file.name, deadline=operation_deadline):
                    if published_cleanup_identity is None:
                        raise OSError("published provider artifact identity was not pinned")
                    _contain_entry_at(
                        download_directory_fd,
                        out_file.name,
                        published_cleanup_identity,
                        "provider-video",
                        deadline=operation_deadline,
                    )
            except (FileNotFoundError, OSError, ValueError):
                result["status"] = "failed"
                result["error"] = "Published provider artifact cleanup was not proven."
                return result
        result["status"] = "failed"
        result["error"] = "Video download failed; check provider logs on the host."
        return result
    finally:
        try:
            _cleanup_temporary(download_directory_fd, temporary_name, download_fd, deadline=operation_deadline)
        except Exception as exc:
            cleanup_error = exc
        if download_fd >= 0:
            try:
                os.close(download_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if download_directory_fd >= 0:
            try:
                os.close(download_directory_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc

    if cleanup_error is not None:
        result["status"] = "failed"
        result["error"] = "Video temporary artifact cleanup was not proven."
        return result

    result.update({"status": "downloaded", "bytes": published_bytes, "sha256": published_sha256, "duration_verified": True})
    return result


def generate_plan(
    video_prompts_path: str | Path,
    output_dir: str | Path,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    clips_dir = output_dir / "clips"
    plan_timeout = _bounded_int_env("SOLO_STUDIO_PROVIDER_OPERATION_TIMEOUT", 7200, 1, 7200)
    operation_deadline = deadline if deadline is not None else time.monotonic() + plan_timeout
    if time.monotonic() >= operation_deadline:
        raise TimeoutError("generation plan deadline exceeded")
    output_directory_fd = -1
    clips_directory_fd = -1
    try:
        output_directory_fd = _open_directory_no_follow(output_dir, create=True)
        clips_directory_fd = _open_directory_no_follow(clips_dir, create=True)
    except (OSError, ValueError):
        return {
            "status": "failed",
            "backend": os.environ.get("SOLO_STUDIO_VIDEO_PROVIDER", "").strip().lower() or "higgsfield",
            "enabled": False,
            "reason": "Output directory is unavailable or unsafe.",
            "setup_needed": "Output directory is unavailable or unsafe.",
            "scenes": [],
            "total_scenes": 0,
        }
    finally:
        if output_directory_fd >= 0:
            os.close(output_directory_fd)
        if clips_directory_fd >= 0:
            os.close(clips_directory_fd)
    try:
        prompts = load_json(video_prompts_path)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        prompts = None
    model = os.environ.get("SOLO_STUDIO_HIGGSFIELD_MODEL", DEFAULT_MODEL)
    profile_error = None
    profile = normalize_output_profile()
    if isinstance(prompts, dict):
        try:
            profile = validate_output_profile_contract(prompts, "video prompts")
        except ValueError as exc:
            profile_error = str(exc)
    binary = shutil.which("higgsfield")
    enable_higgsfield = higgsfield_enabled()
    video_provider = os.environ.get("SOLO_STUDIO_VIDEO_PROVIDER", "").strip().lower()
    if profile_error is None and video_provider not in {"", "higgsfield", "fake"}:
        profile_error = f"Unsupported video provider: {video_provider}"
    fake_mode = video_provider == "fake"
    real_mode = not fake_mode and enable_higgsfield and bool(binary)
    plan: dict[str, Any] = {
        "status": "generating" if real_mode or fake_mode else ("setup_needed" if enable_higgsfield else "dry_run"),
        "backend": video_provider or "higgsfield",
        "enabled": real_mode or fake_mode,
        "reason": None if real_mode or fake_mode else (
            "Set SOLO_STUDIO_ENABLE_HIGGSFIELD=1 and install/authenticate `higgsfield` CLI to generate real clips."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_profile": profile["output_profile"],
        "aspect_ratio": profile["aspect_ratio"],
        "resolution": profile["resolution"],
        "scenes": [],
    }
    plan["setup_needed"] = plan["reason"]

    def write_plan() -> bool:
        if time.monotonic() >= operation_deadline:
            plan["status"] = "failed"
            plan["reason"] = "Generation plan publication deadline exceeded."
            plan["setup_needed"] = plan["reason"]
            return False
        try:
            atomic_write_json(clips_dir / "generation_plan.json", plan, deadline=operation_deadline)
            return True
        except (OSError, TimeoutError, ValueError, subprocess.SubprocessError):
            plan["status"] = "failed"
            plan["reason"] = "Generation plan publication failed."
            plan["setup_needed"] = plan["reason"]
            return False

    if not isinstance(prompts, dict):
        plan["status"] = "failed"
        plan["reason"] = "Video prompts must be a JSON object with a scenes list."
        plan["setup_needed"] = plan["reason"]
        plan["total_scenes"] = 0
        write_plan()
        return plan
    if profile_error:
        plan["status"] = "failed"
        plan["reason"] = profile_error
        plan["setup_needed"] = plan["reason"]
        plan["total_scenes"] = 0
        write_plan()
        return plan
    scenes = prompts.get("scenes", [])
    if not isinstance(scenes, list) or not scenes:
        plan["status"] = "failed"
        plan["reason"] = "No scenes were available: video prompts must contain a non-empty scenes list."
        plan["setup_needed"] = plan["reason"]
        plan["total_scenes"] = 0
        write_plan()
        return plan
    max_scenes = _bounded_int_env("SOLO_STUDIO_MAX_GENERATION_SCENES", 50, 1, 500)
    if len(scenes) > max_scenes:
        plan["status"] = "failed"
        plan["reason"] = "Scene count exceeds the configured generation limit."
        plan["setup_needed"] = plan["reason"]
        plan["total_scenes"] = len(scenes)
        write_plan()
        return plan
    scene_numbers = [scene.get("scene_number") for scene in scenes if isinstance(scene, dict)]
    malformed_numbers = (
        len(scene_numbers) != len(scenes)
        or any(isinstance(number, bool) or not isinstance(number, int) or number <= 0 for number in scene_numbers)
        or len(set(scene_numbers)) != len(scene_numbers)
        or set(scene_numbers) != set(range(1, len(scenes) + 1))
    )
    if malformed_numbers:
        plan["status"] = "failed"
        plan["reason"] = "Scenes must have contiguous unique positive integer scene numbers starting at 1."
        plan["setup_needed"] = plan["reason"]
        plan["total_scenes"] = len(scenes)
        write_plan()
        return plan
    prompt_fields = (
        "visual_description", "kling_prompt", "runway_prompt", "pika_prompt",
        "seedance_prompt", "camera", "visual_style", "transition", "rules", "narration",
    )
    if any(
        key in scene and scene[key] is not None and not isinstance(scene[key], str)
        for scene in scenes
        for key in prompt_fields
    ):
        plan["status"] = "failed"
        plan["reason"] = "Scene prompt fields must be strings when provided."
        plan["setup_needed"] = plan["reason"]
        plan["total_scenes"] = len(scenes)
        write_plan()
        return plan
    durations: list[float] = []
    try:
        for scene in scenes:
            raw_duration = scene.get("duration_seconds", 5)
            if isinstance(raw_duration, bool):
                raise ValueError
            duration = float(raw_duration)
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError
            durations.append(duration)
    except (AttributeError, TypeError, ValueError):
        plan["status"] = "failed"
        plan["reason"] = "Scene durations must be finite positive numbers."
        plan["setup_needed"] = plan["reason"]
        plan["total_scenes"] = len(scenes)
        write_plan()
        return plan
    generation_dir = clips_dir
    staging_dir: Path | None = None
    staging_parent_fd = -1
    staging_fd = -1
    staging_inode: tuple[int, int] | None = None
    staging_identity: tuple[object, ...] | None = None
    if real_mode or fake_mode:
        try:
            staging_parent_fd = _open_directory_no_follow(output_dir, create=False)
            staging_name, staging_fd, captured_staging_inode = cast(
                tuple[str, int, tuple[int, int]],
                _open_private_staging_directory(
                    staging_parent_fd,
                    prefix=f".{clips_dir.name}.generation-",
                    include_identity=True,
                ),
            )
            staging_dir = output_dir / staging_name
            staging_inode = captured_staging_inode
            staging_identity = (captured_staging_inode[0], captured_staging_inode[1], "held-directory")
            generation_dir = staging_dir
        except OSError:
            if staging_parent_fd >= 0:
                if staging_dir is not None and staging_inode is not None and staging_fd >= 0:
                    try:
                        _remove_entry_at(
                            staging_parent_fd,
                            staging_dir.name,
                            (staging_inode[0], staging_inode[1], "held-descriptor"),
                            deadline=operation_deadline,
                            held_fd=staging_fd,
                        )
                    except OSError:
                        pass
                if staging_fd >= 0:
                    os.close(staging_fd)
                    staging_fd = -1
                os.close(staging_parent_fd)
                staging_parent_fd = -1
            elif staging_fd >= 0:
                os.close(staging_fd)
                staging_fd = -1
            plan["status"] = "failed"
            plan["reason"] = "Could not create a safe staging directory for generated clips."
            plan["setup_needed"] = plan["reason"]
            plan["total_scenes"] = len(scenes)
            write_plan()
            return plan
    elif not enable_higgsfield:
        try:
            remove_matching_files(clips_dir, "scene_*.mp4")
        except (OSError, ValueError):
            plan["status"] = "failed"
            plan["reason"] = "The clips directory is not a safe regular directory."
            plan["setup_needed"] = plan["reason"]
            write_plan()
            return plan
    all_downloaded = (real_mode or fake_mode) and bool(scenes)
    if (real_mode or fake_mode) and not scenes:
        plan["status"] = "failed"
        plan["reason"] = "No scenes were available for provider generation."
        plan["setup_needed"] = plan["reason"]
        plan["total_scenes"] = 0
        write_plan()
        return plan
    for scene, duration in zip(scenes, durations):
        number = int(scene.get("scene_number", len(plan["scenes"]) + 1))
        raw_prompt = scene.get("seedance_prompt") or seedance_prompt(scene, profile)
        item: dict[str, Any] = {
            "scene_number": number,
            "duration_seconds": duration,
            "prompt": _redact_persisted_text(raw_prompt),
            "target_file": f"clips/scene_{number:02d}.mp4",
            "status": "pending" if real_mode or fake_mode else "dry_run",
            "output_profile": profile["output_profile"],
            "aspect_ratio": profile["aspect_ratio"],
            "resolution": profile["resolution"],
            "source_prompts": {
                "seedance": _redact_persisted_text(scene.get("seedance_prompt", "")),
                "runway": _redact_persisted_text(scene.get("runway_prompt", "")),
                "pika": _redact_persisted_text(scene.get("pika_prompt", "")),
                "kling": _redact_persisted_text(scene.get("kling_prompt", "")),
            },
        }
        try:
            if fake_mode:
                item.update(run_fake_provider(
                    raw_prompt, duration, generation_dir / f"scene_{number:02d}.mp4", profile,
                    operation_deadline,
                ))
            elif real_mode:
                item.update(run_higgsfield(
                    raw_prompt, duration, generation_dir / f"scene_{number:02d}.mp4", model,
                    profile["aspect_ratio"], profile["width"], profile["height"], operation_deadline,
                ))
        except Exception:
            item.update({"status": "failed", "error": "Provider clip generation failed."})
        if real_mode or fake_mode:
            all_downloaded = all_downloaded and item["status"] == "downloaded"
        plan["scenes"].append(item)
    if real_mode or fake_mode:
        plan["status"] = "completed" if all_downloaded else "failed"
        plan["reason"] = None if all_downloaded else "One or more provider clips failed or were not downloaded."
        plan["setup_needed"] = plan["reason"]
    plan["total_scenes"] = len(plan["scenes"])
    try:
        if staging_dir is not None and all_downloaded:
            if time.monotonic() >= operation_deadline:
                raise TimeoutError("generation plan publication deadline exceeded")
            atomic_write_json(
                generation_dir / "generation_plan.json",
                plan,
                deadline=operation_deadline,
            )
            try:
                _publish_staged_clips_descriptor(
                    staging_dir,
                    clips_dir,
                    staging_fd,
                    deadline=operation_deadline,
                    expected_hashes={
                        "generation_plan.json": _sha256(
                            generation_dir / "generation_plan.json",
                            deadline=operation_deadline,
                        ),
                        **{
                            Path(item["target_file"]).name: item.get("sha256")
                            for item in plan["scenes"]
                        },
                    },
                )
            except ClipPublicationCleanupPending as exc:
                plan["status"] = "failed"
                plan["reason"] = "Generated clips were published but previous clips cleanup is pending."
                plan["setup_needed"] = plan["reason"]
                plan["cleanup_pending"] = exc.backup_name
                write_plan()
                return plan
            # Leave the source staging directory for descriptor-bound cleanup.
        else:
            write_plan()
    except (OSError, TimeoutError, ValueError, subprocess.SubprocessError):
        plan["status"] = "failed"
        plan["reason"] = "Generated clips publication failed."
        plan["setup_needed"] = plan["reason"]
        write_plan()
        return plan
    finally:
        if staging_dir is not None:
            try:
                cleanup_identity = staging_identity
                if staging_parent_fd < 0 or staging_fd < 0:
                    raise OSError("generated clips staging descriptor was not pinned")
                staging_stat = os.fstat(staging_fd)
                cleanup_identity = (staging_stat.st_dev, staging_stat.st_ino, "held-descriptor")
                if time.monotonic() >= operation_deadline:
                    raise TimeoutError("generated clips staging cleanup deadline exceeded")
                _remove_entry_at(
                    staging_parent_fd,
                    staging_dir.name,
                    cleanup_identity,
                    deadline=operation_deadline,
                    held_fd=staging_fd,
                )
                if time.monotonic() >= operation_deadline:
                    raise TimeoutError("generated clips staging cleanup deadline exceeded")
            except (OSError, TimeoutError):
                plan["status"] = "failed"
                plan["reason"] = "Generated clips staging cleanup was not proven."
                plan["setup_needed"] = plan["reason"]
                plan["cleanup_pending"] = staging_dir.name
                try:
                    write_plan()
                except OSError:
                    pass
        if staging_parent_fd >= 0:
            os.close(staging_parent_fd)
        if staging_fd >= 0:
            os.close(staging_fd)
    return plan


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python generation_agent.py <video_prompts.json> [output_dir]", file=sys.stderr)
        return 1
    prompts_path = Path(sys.argv[1])
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else prompts_path.parent
    if not prompts_path.exists():
        print(f"Video prompts not found: {prompts_path}", file=sys.stderr)
        return 1
    plan = generate_plan(prompts_path, output_dir)
    complete = plan["status"] == "completed"
    print(f"Generation plan: {output_dir / 'clips' / 'generation_plan.json'}")
    print(f"Mode: {plan['status']} | scenes={len(plan['scenes'])}")
    if plan.get("reason"):
        print(f"Reason: {plan['reason']}")
    return 0 if plan["status"] == "dry_run" or complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
