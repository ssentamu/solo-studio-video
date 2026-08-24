"""Optional provider-backed voiceover generation with fail-closed output."""
from __future__ import annotations

import json
import http.client
import ipaddress
import os
import socket
import secrets
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from media_assembly import MediaError, probe_media
from package_utils import _open_directory_no_follow


class AudioGenerationError(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise AudioGenerationError("TTS provider redirects are not permitted")


def _public_addresses(hostname: str, port: int) -> list[str]:
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = [literal]
    except ValueError:
        try:
            results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
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

    def connect(self):
        last_error = None
        for address in _public_addresses(self.host, self.port):
            try:
                sock = socket.create_connection((address, self.port), self.timeout)
                context = getattr(self, "_context", None) or ssl.create_default_context()
                self.sock = context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as exc:
                last_error = exc
        raise OSError("TTS provider connection failed") from last_error


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(
            _PinnedHTTPSConnection,
            req,
            context=getattr(self, "_context", None) or ssl.create_default_context(),
        )


def _open_tts_request(request: urllib.request.Request, timeout: int):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
        _PinnedHTTPSHandler(),
    )
    return opener.open(request, timeout=timeout)


def _validate_public_tts_destination(hostname: str, port: int) -> None:
    """Resolve immediately before the request and reject non-public targets."""
    _public_addresses(hostname, port)


def _enabled() -> bool:
    return os.getenv("SOLO_STUDIO_ENABLE_TTS", "0").strip().lower() in {"1", "true", "yes", "on"}


def generate_voiceover(text: str, output: str | Path) -> dict:
    if not _enabled():
        raise AudioGenerationError("TTS is disabled")
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise AudioGenerationError("TTS is enabled but no provider credential is configured")
    if not text.strip():
        raise AudioGenerationError("voiceover script is empty")
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
    _validate_public_tts_destination(parsed_endpoint.hostname, parsed_endpoint.port or 443)
    timeout = max(5, int(os.getenv("SOLO_STUDIO_TTS_TIMEOUT", "120")))
    attempts = max(1, int(os.getenv("SOLO_STUDIO_TTS_ATTEMPTS", "3")))
    max_bytes = max(1024, int(os.getenv("SOLO_STUDIO_MAX_AUDIO_BYTES", str(100 * 1024 * 1024))))
    payload = json.dumps({"text": text, "model_id": os.getenv("SOLO_STUDIO_TTS_MODEL", "eleven_multilingual_v2")}).encode()
    destination = Path(output)
    directory_fd = _open_directory_no_follow(destination.parent, create=True)
    last_error: Exception | None = None
    try:
        for attempt in range(1, attempts + 1):
            temporary_name: str | None = None
            try:
                request = urllib.request.Request(
                    endpoint,
                    data=payload,
                    headers={"Content-Type": "application/json", "Accept": "audio/mpeg", "xi-api-key": api_key},
                    method="POST",
                )
                with _open_tts_request(request, timeout) as response:
                    body = response.read(max_bytes + 1)
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
                    with os.fdopen(temp_fd, "wb") as temporary_stream:
                        temp_fd = -1
                        temporary_stream.write(body)
                        temporary_stream.flush()
                        os.fsync(temporary_stream.fileno())
                finally:
                    if temp_fd >= 0:
                        os.close(temp_fd)
                try:
                    verification_fd = os.open(
                        temporary_name,
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd,
                    )
                    try:
                        metadata = probe_media(
                            Path(f"/proc/self/fd/{directory_fd}/{temporary_name}"),
                            _descriptor=verification_fd,
                        )
                        if not metadata.get("has_audio") or float(metadata.get("duration_seconds", 0)) <= 0:
                            raise AudioGenerationError("provider returned audio that failed media verification")
                        # Publish the exact verified inode.  Unlike rename-by-name,
                        # linking from the held descriptor cannot publish a pathname
                        # that was swapped after verification.  Refuse replacement
                        # of an existing destination; callers must clear stale
                        # generated artifacts before starting a new attempt.
                        os.link(
                            f"/proc/self/fd/{verification_fd}",
                            destination.name,
                            dst_dir_fd=directory_fd,
                            follow_symlinks=True,
                        )
                    finally:
                        os.close(verification_fd)
                except MediaError as exc:
                    raise AudioGenerationError("provider audio failed media verification") from exc
                os.unlink(temporary_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
                return {"path": str(destination), "bytes": len(body), "duration_seconds": metadata["duration_seconds"]}
            except urllib.error.HTTPError as exc:
                if temporary_name is not None:
                    try:
                        os.unlink(temporary_name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                last_error = AudioGenerationError(f"provider HTTP status {exc.code}")
                if exc.code not in {408, 425, 429} and not 500 <= exc.code <= 599:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, AudioGenerationError) as exc:
                if temporary_name is not None:
                    try:
                        os.unlink(temporary_name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                last_error = exc if isinstance(exc, Exception) else AudioGenerationError("provider transport failure")
                if isinstance(exc, AudioGenerationError):
                    break
            if attempt < attempts:
                time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
        raise AudioGenerationError(str(last_error or "voiceover generation failed"))
    finally:
        os.close(directory_fd)
