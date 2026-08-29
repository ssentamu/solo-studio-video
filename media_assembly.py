"""Verified media assembly primitives for Solo Studio.

No caller may publish the final MP4 until every input clip and the assembled
output pass independent media checks.  The functions are provider-agnostic and
use only ffmpeg/ffprobe from the runtime image.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from package_utils import (
    _open_directory_no_follow,
    _open_regular_descriptor,
    _parse_strict_json,
    _publication_lock,
    _remove_entry_at,
    _contain_entry_at,
    _cleanup_identity,
    _entry_cleanup_identity_at,
    _open_private_staging_directory,
    _run_bounded_subprocess,
)


class MediaError(RuntimeError):
    """Raised when media cannot be verified or assembled safely."""


@dataclass(frozen=True)
class PublicationRequest:
    """Descriptor-relative rename request made only after output verification."""

    source_dir_fd: int
    source_name: str
    destination_dir_fd: int
    destination_name: str
    verified: Mapping[str, Any]
    verified_descriptor: int
    verified_inode: tuple[int, int]
    deadline: float | None = None


PublicationCallback = Callable[[PublicationRequest], None]


def _remove_failed_canonical(request: PublicationRequest) -> None:
    """Contain the current canonical entry after a failed publication."""
    try:
        current = os.lstat(request.destination_name, dir_fd=request.destination_dir_fd)
    except FileNotFoundError:
        return
    expected_cleanup_identity = _cleanup_identity(os.fstat(request.verified_descriptor))
    _contain_entry_at(
        request.destination_dir_fd,
        request.destination_name,
        expected_cleanup_identity,
        "failed-media-publication",
        deadline=request.deadline,
    )
    try:
        os.lstat(request.destination_name, dir_fd=request.destination_dir_fd)
    except FileNotFoundError:
        return
    raise OSError("failed publication canonical entry remains present")


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise MediaError(f"{label} is invalid")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaError(f"{label} is invalid") from exc
    if number <= 0:
        raise MediaError(f"{label} is invalid")
    return number


def _unlink_if_unchanged(
    directory_fd: int,
    name: str,
    expected_inode: tuple[object, ...],
    *,
    deadline: float | None = None,
) -> None:
    """Claim and remove only the expected directory entry."""
    _remove_entry_at(directory_fd, name, expected_inode, deadline=deadline)


@contextmanager
def _owned_temporary_directory(parent_fd: int, prefix: str, *, deadline: float | None = None):
    """Create scratch space with a held descriptor and identity-bound cleanup."""
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("assembly scratch creation deadline exceeded")
    temporary_name, temporary_fd = _open_private_staging_directory(parent_fd)
    temporary_path = Path(f"/proc/self/fd/{parent_fd}") / temporary_name
    try:
        temporary_stat = os.fstat(temporary_fd)
        temporary_identity = (temporary_stat.st_dev, temporary_stat.st_ino, "held-directory")
    except BaseException:
        os.close(temporary_fd)
        try:
            _remove_entry_at(parent_fd, temporary_name, deadline=deadline)
        except OSError:
            pass
        raise
    try:
        yield temporary_path, temporary_fd
    finally:
        try:
            _remove_entry_at(parent_fd, temporary_name, temporary_identity, deadline=deadline)
            os.fsync(parent_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(temporary_fd)


def publish_verified_output(request: PublicationRequest) -> None:
    """Serialize cooperating publishers around descriptor-safe publication."""
    with _publication_lock(request.destination_dir_fd, request.destination_name, deadline=request.deadline):
        _publish_verified_output_locked(request)


def _publish_verified_output_locked(request: PublicationRequest) -> None:
    if request.deadline is not None and time.monotonic() >= request.deadline:
        raise TimeoutError("media publication deadline exceeded")
    current = os.fstat(request.verified_descriptor)
    if (current.st_dev, current.st_ino) != request.verified_inode:
        raise MediaError("verified media descriptor no longer refers to the publication inode")
    publication_linked = False
    destination_fd = -1
    try:
        os.link(
            f"/proc/self/fd/{request.verified_descriptor}",
            request.destination_name,
            dst_dir_fd=request.destination_dir_fd,
            follow_symlinks=True,
        )
        publication_linked = True
        _unlink_if_unchanged(
            request.source_dir_fd,
            request.source_name,
            _cleanup_identity(os.fstat(request.verified_descriptor)),
            deadline=request.deadline,
        )
        destination_fd = os.open(
            request.destination_name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=request.destination_dir_fd,
        )
        destination_stat = os.fstat(destination_fd)
        if (destination_stat.st_dev, destination_stat.st_ino) != request.verified_inode:
            raise OSError("publication did not expose the verified media inode")
        published = os.stat(request.destination_name, dir_fd=request.destination_dir_fd, follow_symlinks=False)
        if (published.st_dev, published.st_ino) != request.verified_inode:
            raise OSError("publication pathname does not reference the verified media inode")
        os.fsync(destination_fd)
        if request.deadline is not None and time.monotonic() >= request.deadline:
            raise TimeoutError("media publication deadline exceeded")
        os.fsync(request.destination_dir_fd)
        if request.deadline is not None and time.monotonic() >= request.deadline:
            raise TimeoutError("media publication deadline exceeded")
        after_fsync = os.fstat(destination_fd)
        published_after_fsync = os.stat(
            request.destination_name,
            dir_fd=request.destination_dir_fd,
            follow_symlinks=False,
        )
        if (after_fsync.st_dev, after_fsync.st_ino) != request.verified_inode or (
            published_after_fsync.st_dev,
            published_after_fsync.st_ino,
        ) != request.verified_inode:
            raise OSError("publication changed after durability barrier")
        final_published = os.stat(request.destination_name, dir_fd=request.destination_dir_fd, follow_symlinks=False)
        if (final_published.st_dev, final_published.st_ino) != request.verified_inode:
            raise OSError("publication changed after final identity check")
    except FileExistsError as exc:
        raise MediaError("destination already exists; refusing to overwrite verified media") from exc
    except OSError as exc:
        if publication_linked:
            try:
                _remove_failed_canonical(request)
                os.fsync(request.destination_dir_fd)
            except FileNotFoundError:
                pass
            except OSError as cleanup_exc:
                raise MediaError("publication failed and destination cleanup was not proven") from cleanup_exc
        raise MediaError("verified media publication failed") from exc
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)


def unpublish_verified_output(request: PublicationRequest) -> None:
    """Undo a descriptor-relative publication, idempotently where possible."""
    expected = request.verified_inode
    try:
        source_stat = os.stat(request.source_name, dir_fd=request.source_dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        source_stat = None
    if source_stat is not None:
        if (source_stat.st_dev, source_stat.st_ino) != expected:
            raise MediaError("private publication source was replaced; rollback is not proven")
        # Publication failed before the rename, so leave any pre-existing
        # destination untouched.  The verified attempt-private inode is still
        # available for a later reconciliation decision.
        return
    try:
        destination_stat = os.stat(request.destination_name, dir_fd=request.destination_dir_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise MediaError("verified publication inode is missing; rollback is not proven") from exc
    if (destination_stat.st_dev, destination_stat.st_ino) != expected:
        raise MediaError("published destination was replaced; rollback is not proven")
    try:
        os.link(
            f"/proc/self/fd/{request.verified_descriptor}",
            request.source_name,
            dst_dir_fd=request.source_dir_fd,
            follow_symlinks=True,
        )
        restored = os.stat(request.source_name, dir_fd=request.source_dir_fd, follow_symlinks=False)
        if (restored.st_dev, restored.st_ino) != expected:
            raise MediaError("rollback source does not reference the verified inode")
        _unlink_if_unchanged(
            request.destination_dir_fd,
            request.destination_name,
            _cleanup_identity(destination_stat),
            deadline=request.deadline,
        )
    except FileNotFoundError as exc:
        raise MediaError("verified publication disappeared during rollback") from exc
    except OSError as exc:
        raise MediaError("verified publication rollback failed") from exc
    os.fsync(request.destination_dir_fd)
    if request.source_dir_fd != request.destination_dir_fd:
        os.fsync(request.source_dir_fd)


MEDIA_HASH_MAX_BYTES = 256 * 1024 * 1024


def _tool(name: str) -> str:
    binary = shutil.which(name)
    if not binary:
        raise MediaError(f"{name} is required for media verification")
    return binary


def _sha256_descriptor(
    descriptor: int,
    *,
    deadline: float | None = None,
    max_bytes: int = MEDIA_HASH_MAX_BYTES,
) -> str:
    file_stat = os.fstat(descriptor)
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size < 0 or file_stat.st_size > max_bytes:
        raise MediaError("media artifact exceeds hashing limit")

    def hash_once() -> str:
        digest = hashlib.sha256()
        total = 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("media hashing deadline exceeded")
            total += len(chunk)
            if total > max_bytes:
                raise MediaError("media artifact exceeds hashing limit")
            digest.update(chunk)
        final_stat = os.fstat(descriptor)
        if (final_stat.st_dev, final_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino) or final_stat.st_size != file_stat.st_size:
            raise MediaError("media artifact changed during hashing")
        return digest.hexdigest()

    first_digest = hash_once()
    second_digest = hash_once()
    if first_digest != second_digest:
        raise MediaError("media artifact content changed between hashing passes")
    return second_digest


def _sha256(path: Path, *, deadline: float | None = None) -> str:
    try:
        descriptor = _open_regular_descriptor(path)
    except OSError as exc:
        raise MediaError(f"media artifact is missing: {path.name}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise MediaError(f"media artifact is not a regular file: {path.name}")
        return _sha256_descriptor(descriptor, deadline=deadline)
    finally:
        os.close(descriptor)


def probe_media(
    path: str | Path,
    *,
    timeout: int = 30,
    deadline: float | None = None,
    _descriptor: int | None = None,
) -> dict[str, Any]:
    path = Path(path)
    descriptor_owned = _descriptor is None
    if _descriptor is None:
        try:
            descriptor = _open_regular_descriptor(path)
        except OSError as exc:
            raise MediaError(f"media artifact is missing: {path.name}") from exc
    else:
        descriptor = _descriptor
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise MediaError(f"media artifact is not a regular file: {path.name}")
        if file_stat.st_size <= 0:
            raise MediaError(f"media artifact is empty: {path.name}")
        ffprobe = _tool("ffprobe")
        probe_timeout = float(timeout)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("media probe deadline exceeded")
            probe_timeout = min(probe_timeout, remaining)
        try:
            result = _run_bounded_subprocess(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    f"/proc/self/fd/{descriptor}",
                ],
                timeout=probe_timeout,
                pass_fds=(descriptor,),
                deadline=deadline,
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"ffprobe timed out for {path.name}") from exc
        except subprocess.SubprocessError as exc:
            raise MediaError(f"ffprobe supervision failed for {path.name}") from exc
        except OSError as exc:
            raise MediaError(f"ffprobe could not start for {path.name}") from exc
        if result.returncode != 0:
            raise MediaError(f"ffprobe rejected {path.name}")
        try:
            payload = _parse_strict_json(result.stdout)
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise MediaError(f"ffprobe returned malformed JSON for {path.name}") from exc
        if not isinstance(payload, dict):
            raise MediaError(f"ffprobe returned an invalid payload for {path.name}")
        streams = payload.get("streams")
        fmt = payload.get("format")
        if not isinstance(streams, list) or not isinstance(fmt, dict):
            raise MediaError(f"ffprobe returned incomplete metadata for {path.name}")
        raw_duration = fmt.get("duration")
        try:
            if raw_duration is None:
                raise ValueError
            duration = float(raw_duration)
        except (TypeError, ValueError) as exc:
            raise MediaError(f"media duration is invalid for {path.name}") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise MediaError(f"media duration is not positive for {path.name}")
        return {
            "path": str(path),
            "size_bytes": file_stat.st_size,
            "sha256": _sha256_descriptor(descriptor, deadline=deadline),
            "duration_seconds": duration,
            "format_name": fmt.get("format_name"),
            "streams": streams,
            "has_audio": any(isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams),
            "has_video": any(isinstance(stream, dict) and stream.get("codec_type") == "video" for stream in streams),
        }
    finally:
        if descriptor_owned:
            os.close(descriptor)


def verify_mp4(
    path: str | Path,
    *,
    expected_duration: float | None = None,
    expected_width: int | None = None,
    expected_height: int | None = None,
    tolerance: float = 2.0,
    deadline: float | None = None,
    _descriptor: int | None = None,
) -> dict[str, Any]:
    try:
        tolerance = float(tolerance)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaError("media duration tolerance is invalid") from exc
    if not math.isfinite(tolerance) or tolerance < 0.0:
        raise MediaError("media duration tolerance must be finite and non-negative")
    if expected_duration is not None:
        try:
            expected_duration = float(expected_duration)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MediaError("expected media duration is invalid") from exc
        if not math.isfinite(expected_duration) or expected_duration <= 0.0:
            raise MediaError("expected media duration must be finite and positive")
    path = Path(path)
    if path.suffix.lower() != ".mp4":
        raise MediaError(f"media artifact is not an MP4: {path.name}")
    descriptor_owned = _descriptor is None
    try:
        descriptor = _open_regular_descriptor(path) if _descriptor is None else _descriptor
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise MediaError(f"media artifact is not a regular file: {path.name}")
        header = os.read(descriptor, 12)
        if len(header) < 8 or header[4:8] != b"ftyp":
            raise MediaError(f"media artifact has no MP4 signature: {path.name}")
        metadata = probe_media(path, deadline=deadline, _descriptor=descriptor)
    except OSError as exc:
        raise MediaError(f"media artifact is missing: {path.name}") from exc
    finally:
        if descriptor_owned and "descriptor" in locals():
            os.close(descriptor)
    video_streams = [stream for stream in metadata["streams"] if isinstance(stream, dict) and stream.get("codec_type") == "video"]
    if not video_streams:
        raise MediaError(f"media artifact has no video stream: {path.name}")
    video_stream = video_streams[0]
    width = _positive_int(video_stream.get("width"), "media width")
    height = _positive_int(video_stream.get("height"), "media height")
    if expected_duration is not None:
        if not math.isfinite(float(metadata["duration_seconds"])) or abs(metadata["duration_seconds"] - expected_duration) > tolerance:
            raise MediaError(f"media duration is outside the expected range: {path.name}")
    if expected_width is not None and width != _positive_int(expected_width, "expected media width"):
        raise MediaError(f"media width does not match the expected profile: {path.name}")
    if expected_height is not None and height != _positive_int(expected_height, "expected media height"):
        raise MediaError(f"media height does not match the expected profile: {path.name}")
    metadata["width"] = width
    metadata["height"] = height
    metadata["video_codec"] = video_stream.get("codec_name")
    metadata["valid"] = True
    return metadata


def _concat_manifest(clips: list[Path], directory: Path) -> Path:
    manifest = directory / "concat.txt"
    lines = []
    for clip in clips:
        # The paths are generated under the job directory.  ffmpeg's concat
        # syntax accepts single-quote escaping; do not pass shell text through.
        escaped = str(clip.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def assemble_verified_clips(
    clips: Iterable[str | Path],
    output: str | Path,
    *,
    expected_duration: float | None = None,
    expected_width: int | None = None,
    expected_height: int | None = None,
    timeout: int = 900,
    lease_check: Callable[[], None] | None = None,
    publication_callback: PublicationCallback | None = None,
    expected_clip_sha256: Mapping[str, str] | None = None,
    expected_clip_durations: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Concatenate verified clips and atomically publish a verified final MP4."""
    operation_deadline = time.monotonic() + float(timeout)
    if operation_deadline <= time.monotonic():
        raise MediaError("media assembly deadline exceeded")
    if lease_check is not None:
        lease_check()
    clip_paths = [Path(clip) for clip in clips]
    if not clip_paths:
        raise MediaError("cannot assemble a video without clips")
    if len({path.resolve() for path in clip_paths}) != len(clip_paths):
        raise MediaError("duplicate clip paths are not allowed")
    for clip in clip_paths:
        if lease_check is not None:
            lease_check()
        expected_clip_duration = None
        if expected_clip_durations is not None:
            expected_clip_duration = expected_clip_durations.get(clip.name)
            if expected_clip_duration is None:
                raise MediaError(f"clip duration contract is missing: {clip.name}")
        verify_mp4(
            clip,
            expected_duration=expected_clip_duration,
            expected_width=expected_width,
            expected_height=expected_height,
            tolerance=0.5,
            deadline=operation_deadline,
        )

    output = Path(output)
    output_parent_fd = _open_directory_no_follow(output.parent, create=True)
    output_parent_path = Path(f"/proc/self/fd/{output_parent_fd}")
    ffmpeg = _tool("ffmpeg")
    temporary_output_name = "assembled.mp4"
    published_fd = -1
    try:
        with _owned_temporary_directory(output_parent_fd, f".{output.stem}-", deadline=operation_deadline) as (temp, temp_dir_fd):
            stable_clips: list[Path] = []
            try:
                for index, clip in enumerate(clip_paths):
                    if lease_check is not None:
                        lease_check()
                    expected_clip_duration = None
                    if expected_clip_durations is not None:
                        expected_clip_duration = expected_clip_durations.get(clip.name)
                        if expected_clip_duration is None:
                            raise MediaError(f"clip duration contract is missing: {clip.name}")
                    source_fd = -1
                    destination_fd = -1
                    try:
                        source_fd = _open_regular_descriptor(clip)
                        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                            raise MediaError(f"media artifact is not a regular file: {clip.name}")
                        stable_name = f"input_{index:04d}.mp4"
                        stable_path = temp / stable_name
                        destination_fd = os.open(
                            stable_name,
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0),
                            0o600,
                            dir_fd=temp_dir_fd,
                        )
                        while True:
                            if operation_deadline is not None and time.monotonic() >= operation_deadline:
                                raise MediaError("media snapshot deadline exceeded")
                            if lease_check is not None:
                                lease_check()
                            chunk = os.read(source_fd, 1024 * 1024)
                            if not chunk:
                                break
                            view = memoryview(chunk)
                            while view:
                                if operation_deadline is not None and time.monotonic() >= operation_deadline:
                                    raise MediaError("media snapshot deadline exceeded")
                                written = os.write(destination_fd, view)
                                view = view[written:]
                        os.fsync(destination_fd)
                        stable_clips.append(stable_path)
                    except OSError as exc:
                        raise MediaError(f"could not snapshot media artifact: {clip.name}") from exc
                    finally:
                        if destination_fd >= 0:
                            os.close(destination_fd)
                        if source_fd >= 0:
                            os.close(source_fd)
                    stable_fd = -1
                    try:
                        stable_fd = os.open(
                            stable_name,
                            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=temp_dir_fd,
                        )
                        verify_mp4(
                            stable_path,
                            expected_duration=expected_clip_duration,
                            expected_width=expected_width,
                            expected_height=expected_height,
                            _descriptor=stable_fd,
                            deadline=operation_deadline,
                        )
                        if expected_clip_sha256 is not None:
                            expected_digest = expected_clip_sha256.get(clip.name)
                            if expected_digest is None or _sha256_descriptor(stable_fd, deadline=operation_deadline) != expected_digest:
                                raise MediaError(f"clip hash does not match the generation plan: {clip.name}")
                    finally:
                        if stable_fd >= 0:
                            os.close(stable_fd)
                    if lease_check is not None:
                        lease_check()

                concat = _concat_manifest(stable_clips, temp)
                temporary_output = temp / temporary_output_name
                if lease_check is not None:
                    lease_check()
                try:
                    remaining = operation_deadline - time.monotonic()
                    if remaining <= 0:
                        raise MediaError("media assembly deadline exceeded")
                    result = _run_bounded_subprocess(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(concat),
                        "-c",
                        "copy",
                        str(temporary_output),
                    ],
                    timeout=remaining,
                    pass_fds=(output_parent_fd,),
                    deadline=operation_deadline,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise MediaError("ffmpeg assembly timed out") from exc
                except subprocess.SubprocessError as exc:
                    raise MediaError("ffmpeg assembly supervision failed") from exc
                except OSError as exc:
                    raise MediaError("ffmpeg could not start") from exc
                if result.returncode != 0:
                    raise MediaError("ffmpeg assembly failed")
                temporary_output_fd = os.open(
                    temporary_output_name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=temp_dir_fd,
                )
                try:
                    verified = verify_mp4(
                        temporary_output,
                        expected_duration=expected_duration,
                        expected_width=expected_width,
                        expected_height=expected_height,
                        tolerance=0.5,
                        _descriptor=temporary_output_fd,
                        deadline=operation_deadline,
                    )
                    descriptor_digest = _sha256_descriptor(temporary_output_fd, deadline=operation_deadline)
                    if verified.get("sha256") not in (None, descriptor_digest):
                        raise MediaError("verified media hash does not match its held descriptor")
                    verified["sha256"] = descriptor_digest
                    verified_stat = os.fstat(temporary_output_fd)
                    request = PublicationRequest(
                        source_dir_fd=temp_dir_fd,
                        source_name=temporary_output_name,
                        destination_dir_fd=output_parent_fd,
                        destination_name=output.name,
                        verified=verified,
                        verified_descriptor=temporary_output_fd,
                        verified_inode=(verified_stat.st_dev, verified_stat.st_ino),
                        deadline=operation_deadline,
                    )
                    if lease_check is not None:
                        lease_check()
                    try:
                        if publication_callback is None:
                            publish_verified_output(request)
                        else:
                            publication_callback(request)
                    except Exception as exc:
                        try:
                            unpublish_verified_output(request)
                        except Exception as rollback_exc:
                            raise MediaError(f"final publication failed and rollback was not proven: {rollback_exc}") from exc
                        raise
                    published_fd = os.dup(request.verified_descriptor)
                finally:
                    os.close(temporary_output_fd)
            finally:
                pass

        try:
            published_stat = os.fstat(published_fd)
            if (published_stat.st_dev, published_stat.st_ino) != request.verified_inode:
                raise MediaError("published output inode changed during temporary cleanup")
            published_digest = _sha256_descriptor(published_fd, deadline=operation_deadline)
            if published_digest != verified.get("sha256"):
                raise MediaError("published output changed during temporary cleanup")
        finally:
            os.close(published_fd)
            published_fd = -1

        # The evidence is rechecked after temporary cleanup, which can invoke
        # filesystem callbacks in tests or reconciliation code.
        verified["path"] = str(output)
        return verified
    finally:
        if published_fd >= 0:
            os.close(published_fd)
        os.close(output_parent_fd)
