"""Optional provider-backed voiceover generation with fail-closed output."""
from __future__ import annotations

import json
import http.client
import ipaddress
import math
import multiprocessing
import os
import socket
import secrets
import ssl
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from media_assembly import MediaError, probe_media
from package_utils import _cleanup_identity, _contain_entry_at, _fsync_verified_publication, _open_directory_no_follow, _publication_lock, _remove_entry_at, _set_response_timeout


class AudioGenerationError(RuntimeError):
    pass


_TTS_DNS_SLOT = threading.BoundedSemaphore(1)


def _dns_resolve_worker(hostname: str, port: int, result_pipe) -> None:
    try:
        try:
            result_pipe.send(("ok", socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)))
        except BaseException as exc:
            result_pipe.send(("error", type(exc).__name__))
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        result_pipe.close()


def _reap_dns_worker(process, *, deadline: float | None = None, force: bool = False) -> bool:
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


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, value))


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise AudioGenerationError("TTS provider redirects are not permitted")


def _public_addresses(hostname: str, port: int, *, deadline: float | None = None) -> list[str]:
    if deadline is not None and time.monotonic() >= deadline:
        raise AudioGenerationError("TTS hostname resolution deadline exceeded")
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = [literal]
    except ValueError:
        try:
            if deadline is None:
                results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            else:
                if not _TTS_DNS_SLOT.acquire(blocking=False):
                    raise AudioGenerationError("TTS hostname resolution capacity exhausted")
                parent_pipe = child_pipe = None
                resolver = None
                resolver_started = False
                try:
                    parent_pipe, child_pipe = multiprocessing.Pipe(duplex=False)
                    try:
                        context = multiprocessing.get_context("fork")
                    except ValueError as exc:
                        raise AudioGenerationError("bounded DNS resolution is unavailable") from exc
                    resolver = context.Process(
                        target=_dns_resolve_worker,
                        args=(hostname, port, child_pipe),
                        name="tts-dns",
                    )
                    resolver.daemon = True
                    resolver.start()
                    resolver_started = True
                    child_pipe.close()
                    child_pipe = None
                    remaining = max(0.0, deadline - time.monotonic())
                    if not parent_pipe.poll(remaining):
                        if not _reap_dns_worker(resolver, deadline=deadline, force=True):
                            raise AudioGenerationError("TTS DNS resolver cleanup was not proven")
                        raise AudioGenerationError("TTS hostname resolution deadline exceeded")
                    try:
                        outcome, payload = parent_pipe.recv()
                    except EOFError as exc:
                        raise AudioGenerationError("TTS hostname could not be resolved") from exc
                    if resolver_started and resolver.is_alive():
                        resolver.join(max(0.0, deadline - time.monotonic()))
                    if resolver_started and resolver.is_alive() and not _reap_dns_worker(
                        resolver, deadline=deadline, force=True
                    ):
                        raise AudioGenerationError("TTS DNS resolver cleanup was not proven")
                    if outcome == "error":
                        raise AudioGenerationError("TTS hostname could not be resolved")
                    if outcome != "ok" or not isinstance(payload, list):
                        raise AudioGenerationError("TTS hostname could not be resolved")
                    results = payload
                finally:
                    if parent_pipe is not None:
                        parent_pipe.close()
                    if child_pipe is not None:
                        try:
                            child_pipe.close()
                        except OSError:
                            pass
                    if resolver_started and resolver is not None and resolver.is_alive():
                        _reap_dns_worker(resolver, deadline=deadline, force=True)
                    _TTS_DNS_SLOT.release()
        except AudioGenerationError:
            raise
        except OSError as exc:
            raise AudioGenerationError("TTS hostname could not be resolved") from exc
        addresses = []
        for result in results:
            address = ipaddress.ip_address(result[4][0])
            if address not in addresses:
                addresses.append(address)
    if not addresses or any(not address.is_global for address in addresses):
        raise AudioGenerationError("TTS endpoint resolved to a non-public address")
    return [str(address) for address in addresses]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a freshly resolved public IP while retaining TLS SNI."""

    def __init__(self, host: str, *, deadline: float | None = None, **kwargs):
        super().__init__(host, **kwargs)
        self.deadline = deadline

    def connect(self):
        connection_deadline = self.deadline if self.deadline is not None else time.monotonic() + (float(self.timeout) if self.timeout is not None else 120.0)
        last_error = None
        for address in _public_addresses(self.host, self.port, deadline=connection_deadline):
            try:
                remaining = connection_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("TTS connection deadline exceeded")
                sock = socket.create_connection((address, self.port), remaining)
                remaining = connection_deadline - time.monotonic()
                if remaining <= 0:
                    sock.close()
                    raise TimeoutError("TTS TLS deadline exceeded")
                sock.settimeout(remaining)
                context = getattr(self, "_context", None) or ssl.create_default_context()
                self.sock = context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as exc:
                last_error = exc
        raise OSError("TTS provider connection failed") from last_error


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline: float | None = None):
        super().__init__()
        self.deadline = deadline

    def https_open(self, req):
        def connection_factory(host, **kwargs):
            remaining = self.deadline - time.monotonic() if self.deadline is not None else kwargs.get("timeout")
            if remaining is None or remaining <= 0:
                raise TimeoutError("TTS connection deadline exceeded")
            kwargs["timeout"] = remaining
            return _PinnedHTTPSConnection(host, deadline=self.deadline, **kwargs)

        return self.do_open(
            connection_factory,
            req,
            context=getattr(self, "_context", None) or ssl.create_default_context(),
        )


def _open_tts_request(request: urllib.request.Request, timeout: float, *, deadline: float | None = None):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
        _PinnedHTTPSHandler(deadline),
    )
    return opener.open(request, timeout=timeout)


def _validate_public_tts_destination(hostname: str, port: int, *, deadline: float | None = None) -> None:
    """Resolve immediately before the request and reject non-public targets."""
    _public_addresses(hostname, port, deadline=deadline)


def _enabled() -> bool:
    return os.getenv("SOLO_STUDIO_ENABLE_TTS", "0").strip().lower() in {"1", "true", "yes", "on"}


def generate_voiceover(
    text: str, output: str | Path, *, max_duration_seconds: float | None = None,
    deadline: float | None = None,
) -> dict:
    if not _enabled():
        raise AudioGenerationError("TTS is disabled")
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise AudioGenerationError("TTS is enabled but no provider credential is configured")
    if not isinstance(text, str):
        raise AudioGenerationError("voiceover script must be text")
    if not text.strip():
        raise AudioGenerationError("voiceover script is empty")
    max_characters = _bounded_int_env("SOLO_STUDIO_TTS_MAX_CHARACTERS", 20000, 1, 200000)
    max_text_bytes = _bounded_int_env("SOLO_STUDIO_TTS_MAX_TEXT_BYTES", 256 * 1024, 1, 2 * 1024 * 1024)
    if len(text) > max_characters:
        raise AudioGenerationError("voiceover script exceeds the configured character limit")
    try:
        encoded_text = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AudioGenerationError("voiceover script is not valid UTF-8 text") from exc
    if len(encoded_text) > max_text_bytes:
        raise AudioGenerationError("voiceover script exceeds the configured byte limit")
    voice_id = os.getenv("SOLO_STUDIO_TTS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM").strip()
    endpoint = os.getenv(
        "SOLO_STUDIO_TTS_ENDPOINT",
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
    ).strip()
    parsed_endpoint = urllib.parse.urlsplit(endpoint)
    allowed_hosts = {
        value.strip().lower()
        for value in os.getenv("SOLO_STUDIO_TTS_ALLOWED_HOSTS", "api.elevenlabs.io").split(",")
        if value.strip()
    }
    if (
        parsed_endpoint.scheme != "https"
        or not parsed_endpoint.hostname
        or parsed_endpoint.hostname.lower() not in allowed_hosts
        or parsed_endpoint.port not in {None, 443}
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
    ):
        raise AudioGenerationError("TTS endpoint must use an explicitly trusted HTTPS host")
    timeout = _bounded_int_env("SOLO_STUDIO_TTS_TIMEOUT", 120, 5, 3600)
    operation_deadline = deadline if deadline is not None else time.monotonic() + timeout
    _validate_public_tts_destination(parsed_endpoint.hostname, parsed_endpoint.port or 443, deadline=operation_deadline)
    max_bytes = _bounded_int_env("SOLO_STUDIO_MAX_AUDIO_BYTES", 100 * 1024 * 1024, 1024, 2 * 1024 * 1024 * 1024)
    configured_max_duration = _bounded_int_env("SOLO_STUDIO_TTS_MAX_DURATION_SECONDS", 900, 1, 3600)
    max_duration = configured_max_duration if max_duration_seconds is None else min(float(max_duration_seconds), configured_max_duration)
    if max_duration <= 0 or not math.isfinite(max_duration):
        raise AudioGenerationError("TTS duration bound must be finite and positive")
    payload = json.dumps({"text": text, "model_id": os.getenv("SOLO_STUDIO_TTS_MODEL", "eleven_multilingual_v2")}).encode()
    destination = Path(output)
    directory_fd = _open_directory_no_follow(destination.parent, create=True)
    last_error: Exception | None = None
    try:
        # A POST that returned an ambiguous/transient response may already have
        # consumed credits. Never retry the billable submission automatically.
        for attempt in range(1, 2):
            temporary_name: str | None = None
            temporary_inode: tuple[int, int] | None = None
            temporary_cleanup_identity: tuple[object, ...] | None = None
            destination_linked = False
            published_cleanup_identity: tuple[object, ...] | None = None
            response_acquired = False
            try:
                request = urllib.request.Request(
                    endpoint,
                    data=payload,
                    headers={"Content-Type": "application/json", "Accept": "audio/mpeg", "xi-api-key": api_key},
                    method="POST",
                )
                remaining = operation_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("TTS provider operation deadline exceeded")
                response = _open_tts_request(request, min(float(timeout), remaining), deadline=operation_deadline)
                response_acquired = True
                response_deadline = operation_deadline
                chunks: list[bytes] = []
                total = 0
                with response:
                    while True:
                        remaining_bytes = max_bytes + 1 - total
                        if remaining_bytes <= 0:
                            break
                        remaining = response_deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("TTS provider response deadline exceeded")
                        _set_response_timeout(response, remaining)
                        chunk = response.read(min(1024 * 1024, remaining_bytes))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if time.monotonic() >= response_deadline:
                            raise TimeoutError("TTS provider response deadline exceeded")
                body = b"".join(chunks)
                if time.monotonic() >= operation_deadline:
                    raise TimeoutError("TTS provider operation deadline exceeded")
                if len(body) == 0:
                    raise AudioGenerationError("provider returned an empty audio artifact")
                if len(body) > max_bytes:
                    raise AudioGenerationError("provider audio artifact exceeds configured size limit")
                temporary_name = f".{destination.name}.part-{os.getpid()}-{secrets.token_hex(16)}"
                temp_fd = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    if not stat.S_ISREG(os.fstat(temp_fd).st_mode):
                        raise AudioGenerationError("provider temporary artifact is not a regular file")
                    written_stat = os.fstat(temp_fd)
                    temporary_inode = (written_stat.st_dev, written_stat.st_ino)
                    with os.fdopen(temp_fd, "wb") as temporary_stream:
                        temp_fd = -1
                        temporary_stream.write(body)
                        temporary_stream.flush()
                        written_after_write_stat = os.fstat(temporary_stream.fileno())
                        temporary_cleanup_identity = _cleanup_identity(written_after_write_stat)
                        os.fsync(temporary_stream.fileno())
                finally:
                    if temp_fd >= 0:
                        os.close(temp_fd)
                try:
                    verification_fd = os.open(
                        temporary_name,
                        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd,
                    )
                    try:
                        verification_stat = os.fstat(verification_fd)
                        if (verification_stat.st_dev, verification_stat.st_ino) != temporary_inode:
                            raise AudioGenerationError("provider temporary artifact was replaced before verification")
                        temporary_cleanup_identity = _cleanup_identity(verification_stat)
                        metadata = probe_media(
                            Path(f"/proc/self/fd/{directory_fd}/{temporary_name}"),
                            _descriptor=verification_fd,
                            deadline=operation_deadline,
                        )
                        if (
                            not metadata.get("has_audio")
                            or metadata.get("has_video")
                            or float(metadata.get("duration_seconds", 0)) <= 0
                        ):
                            raise AudioGenerationError("provider returned audio that failed media verification")
                        if float(metadata["duration_seconds"]) > max_duration:
                            raise AudioGenerationError("provider audio duration exceeds the configured bound")
                        if time.monotonic() >= operation_deadline:
                            raise TimeoutError("TTS provider operation deadline exceeded")
                        # Publish the exact verified inode.  Unlike rename-by-name,
                        # linking from the held descriptor cannot publish a pathname
                        # that was swapped after verification.  Refuse replacement
                        # of an existing destination; callers must clear stale
                        # generated artifacts before starting a new attempt.
                        with _publication_lock(directory_fd, destination.name, deadline=operation_deadline):
                            os.link(
                                f"/proc/self/fd/{verification_fd}",
                                destination.name,
                                dst_dir_fd=directory_fd,
                                follow_symlinks=True,
                            )
                            destination_linked = True
                            published_cleanup_identity = _cleanup_identity(os.lstat(destination.name, dir_fd=directory_fd))
                            if temporary_cleanup_identity is None:
                                raise OSError("provider temporary cleanup identity was not pinned")
                            _remove_entry_at(
                                directory_fd,
                                temporary_name,
                                temporary_cleanup_identity,
                                deadline=operation_deadline,
                                verify_preclaim_ctime=False,
                            )
                            _fsync_verified_publication(directory_fd, destination.name, temporary_inode, deadline=operation_deadline)
                            if time.monotonic() >= operation_deadline:
                                raise TimeoutError("TTS provider operation deadline exceeded")
                            return {"path": str(destination), "bytes": len(body), "duration_seconds": metadata["duration_seconds"]}
                    finally:
                        os.close(verification_fd)
                except MediaError as exc:
                    raise AudioGenerationError("provider audio failed media verification") from exc

            except urllib.error.HTTPError as exc:
                if destination_linked and published_cleanup_identity is not None:
                    try:
                        with _publication_lock(directory_fd, destination.name, deadline=operation_deadline):
                            _contain_entry_at(
                                directory_fd,
                                destination.name,
                                published_cleanup_identity,
                                "tts-audio",
                                deadline=operation_deadline,
                            )
                    except (FileNotFoundError, OSError, ValueError) as cleanup_exc:
                        raise AudioGenerationError("provider audio rollback was not proven") from cleanup_exc
                if temporary_name is not None and temporary_inode is not None:
                    try:
                        if temporary_cleanup_identity is None:
                            raise OSError("provider temporary cleanup identity was not pinned")
                        _remove_entry_at(
                            directory_fd,
                            temporary_name,
                            temporary_cleanup_identity,
                            deadline=operation_deadline,
                            verify_preclaim_ctime=False,
                        )
                    except (FileNotFoundError, OSError):
                        pass
                last_error = AudioGenerationError(f"provider HTTP status {exc.code}")
                if exc.code not in {408, 425, 429} and not 500 <= exc.code <= 599:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, AudioGenerationError, MediaError) as exc:
                if destination_linked and published_cleanup_identity is not None:
                    try:
                        with _publication_lock(directory_fd, destination.name, deadline=operation_deadline):
                            _contain_entry_at(
                                directory_fd,
                                destination.name,
                                published_cleanup_identity,
                                "tts-audio",
                                deadline=operation_deadline,
                            )
                    except (FileNotFoundError, OSError, ValueError) as cleanup_exc:
                        raise AudioGenerationError("provider audio rollback was not proven") from cleanup_exc
                if temporary_name is not None and temporary_inode is not None:
                    try:
                        if temporary_cleanup_identity is None:
                            raise OSError("provider temporary cleanup identity was not pinned")
                        _remove_entry_at(
                            directory_fd,
                            temporary_name,
                            temporary_cleanup_identity,
                            deadline=operation_deadline,
                            verify_preclaim_ctime=False,
                        )
                    except (FileNotFoundError, OSError):
                        pass
                if isinstance(exc, AudioGenerationError):
                    last_error = exc
                elif isinstance(exc, MediaError):
                    last_error = AudioGenerationError("provider audio failed media verification")
                elif isinstance(exc, TimeoutError):
                    last_error = AudioGenerationError("provider response deadline exceeded")
                elif isinstance(exc, urllib.error.URLError):
                    last_error = AudioGenerationError("provider transport failed")
                else:
                    last_error = AudioGenerationError("local audio artifact operation failed")
                if isinstance(exc, AudioGenerationError) or isinstance(exc, MediaError):
                    break
                if isinstance(exc, OSError) and response_acquired:
                    break
            raise AudioGenerationError(str(last_error or "voiceover generation failed"))
        raise AudioGenerationError(str(last_error or "voiceover generation failed"))
    finally:
        os.close(directory_fd)
