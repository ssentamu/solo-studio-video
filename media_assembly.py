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
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from package_utils import _open_directory_no_follow, _open_regular_descriptor


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


PublicationCallback = Callable[[PublicationRequest], None]


def publish_verified_output(request: PublicationRequest) -> None:
    """Perform the existing descriptor-safe atomic publication operation."""
    current = os.fstat(request.verified_descriptor)
    if (current.st_dev, current.st_ino) != request.verified_inode:
        raise MediaError("verified media descriptor no longer refers to the publication inode")
    os.replace(
        request.source_name,
        request.destination_name,
        src_dir_fd=request.source_dir_fd,
        dst_dir_fd=request.destination_dir_fd,
    )
    published = os.stat(request.destination_name, dir_fd=request.destination_dir_fd, follow_symlinks=False)
    if (published.st_dev, published.st_ino) != request.verified_inode:
        raise MediaError("publication did not expose the verified media inode")
    os.fsync(request.destination_dir_fd)


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
        os.replace(
            request.destination_name,
            request.source_name,
            src_dir_fd=request.destination_dir_fd,
            dst_dir_fd=request.source_dir_fd,
        )
    except FileNotFoundError as exc:
        raise MediaError("verified publication disappeared during rollback") from exc
    os.fsync(request.destination_dir_fd)
    if request.source_dir_fd != request.destination_dir_fd:
        os.fsync(request.source_dir_fd)


def _tool(name: str) -> str:
    binary = shutil.which(name)
    if not binary:
        raise MediaError(f"{name} is required for media verification")
    return binary


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    try:
        descriptor = _open_regular_descriptor(path)
    except OSError as exc:
        raise MediaError(f"media artifact is missing: {path.name}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise MediaError(f"media artifact is not a regular file: {path.name}")
        return _sha256_descriptor(descriptor)
    finally:
        os.close(descriptor)


def probe_media(path: str | Path, *, timeout: int = 30, _descriptor: int | None = None) -> dict[str, Any]:
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
        try:
            result = subprocess.run(
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
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                pass_fds=(descriptor,),
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"ffprobe timed out for {path.name}") from exc
        except OSError as exc:
            raise MediaError(f"ffprobe could not start for {path.name}") from exc
        if result.returncode != 0:
            raise MediaError(f"ffprobe rejected {path.name}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
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
            "sha256": _sha256_descriptor(descriptor),
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
    tolerance: float = 2.0,
    _descriptor: int | None = None,
) -> dict[str, Any]:
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
        metadata = probe_media(path, _descriptor=descriptor)
    except OSError as exc:
        raise MediaError(f"media artifact is missing: {path.name}") from exc
    finally:
        if descriptor_owned and "descriptor" in locals():
            os.close(descriptor)
    video_streams = [stream for stream in metadata["streams"] if isinstance(stream, dict) and stream.get("codec_type") == "video"]
    if not video_streams:
        raise MediaError(f"media artifact has no video stream: {path.name}")
    if expected_duration is not None:
        try:
            expected = float(expected_duration)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MediaError("expected media duration is invalid") from exc
        if not math.isfinite(expected) or abs(metadata["duration_seconds"] - expected) > tolerance:
            raise MediaError(f"media duration is outside the expected range: {path.name}")
    metadata["video_codec"] = video_streams[0].get("codec_name")
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
    timeout: int = 900,
    lease_check: Callable[[], None] | None = None,
    publication_callback: PublicationCallback | None = None,
    expected_clip_sha256: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Concatenate verified clips and atomically publish a verified final MP4."""
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
        verify_mp4(clip)

    output = Path(output)
    output_parent_fd = _open_directory_no_follow(output.parent, create=True)
    output_parent_path = Path(f"/proc/self/fd/{output_parent_fd}")
    ffmpeg = _tool("ffmpeg")
    temporary_output_name = "assembled.mp4"
    try:
        with tempfile.TemporaryDirectory(prefix=f".{output.stem}-", dir=output_parent_path) as temp_dir:
            temp = Path(temp_dir)
            temp_dir_fd = os.open(
                temp.name.rsplit("/", 1)[-1],
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=output_parent_fd,
            )
            stable_clips: list[Path] = []
            try:
                for index, clip in enumerate(clip_paths):
                    if lease_check is not None:
                        lease_check()
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
                            chunk = os.read(source_fd, 1024 * 1024)
                            if not chunk:
                                break
                            view = memoryview(chunk)
                            while view:
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
                        verify_mp4(stable_path, _descriptor=stable_fd)
                        if expected_clip_sha256 is not None:
                            expected_digest = expected_clip_sha256.get(clip.name)
                            if expected_digest is None or _sha256_descriptor(stable_fd) != expected_digest:
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
                    result = subprocess.run(
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
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise MediaError("ffmpeg assembly timed out") from exc
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
                        _descriptor=temporary_output_fd,
                    )
                    descriptor_digest = _sha256_descriptor(temporary_output_fd)
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
                finally:
                    os.close(temporary_output_fd)
            finally:
                os.close(temp_dir_fd)

        # ``verified`` was computed from the held descriptor before the
        # descriptor-relative rename.  Do not reopen ``output`` here: a
        # post-publication pathname read can observe a replacement after the
        # lease barrier and commit evidence for the wrong inode.
        verified["path"] = str(output)
        return verified
    finally:
        os.close(output_parent_fd)
