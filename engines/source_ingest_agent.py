"""Safe, deterministic URL source ingestion for reverse briefs.

This module intentionally uses only the Python standard library.  Network
access is kept behind small seams (``resolver`` and ``opener``) so callers can
unit-test the security boundary without making live requests.
"""
from __future__ import annotations

import hashlib
import html
import http.client
import ipaddress
import json
import math
import os
import re
import signal
import socket
import ssl
import stat
import sys
import threading
import time
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request

from package_utils import (
    _cleanup_identity,
    _contain_entry_at,
    _entry_cleanup_identity_at,
    _open_directory_no_follow,
    _open_regular_descriptor,
    _regular_content_digest_at,
    _rename_noreplace,
    _remove_entry_at,
    atomic_write_json,
    atomic_write_text,
)

MAX_REFERENCE_URLS = 3
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_BODY_BYTES = 512 * 1024
DEFAULT_CHUNK_SIZE = 16 * 1024
MAX_TEXT_CHARS = 20_000
MAX_HEADINGS = 40
MAX_PARAGRAPHS = 100
MAX_BRIEF_BYTES = 1_048_576
MAX_RESOLVER_THREADS = 4
CLEANUP_GRACE_SECONDS = 1.0
_RESOLVER_SLOTS = threading.BoundedSemaphore(MAX_RESOLVER_THREADS)


class SourceIngestError(RuntimeError):
    """A safe, stable source-ingestion failure.

    The exception deliberately contains only a fixed classification.  Raw
    URLs, response bodies, and underlying exception messages never cross this
    boundary.
    """

    def __init__(self, code: str):
        self.code = code if code in ERROR_CODES else "ingestion_error"
        super().__init__(self.code)


ERROR_CODES = frozenset(
    {
        "invalid_url",
        "blocked_address",
        "dns_error",
        "timeout",
        "network_error",
        "http_error",
        "redirect_rejected",
        "redirect_limit",
        "body_too_large",
        "malformed_response",
        "unsupported_media",
        "extraction_error",
        "write_error",
        "ingestion_error",
    }
)


def _invalid(code: str) -> SourceIngestError:
    return SourceIngestError(code)


def _address_is_public(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped
    return not any(
        (
            parsed.is_loopback,
            parsed.is_private,
            parsed.is_link_local,
            parsed.is_multicast,
            parsed.is_unspecified,
            parsed.is_reserved,
            not parsed.is_global,
        )
    )


def resolve_hostname(hostname: str) -> list[str]:
    """Resolve a hostname to unique address strings using the system resolver."""
    try:
        results = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        del exc
        raise _invalid("dns_error")
    addresses: list[str] = []
    for result in results:
        sockaddr = result[4] if len(result) > 4 else ()
        address = sockaddr[0] if sockaddr else ""
        if isinstance(address, str) and address not in addresses:
            addresses.append(address)
    if not addresses:
        raise _invalid("dns_error")
    return addresses


def _call_with_deadline(callable_: Callable[[], Any], deadline: float | None) -> Any:
    """Run a resolver call without allowing it to outlive the fetch deadline."""
    if deadline is None:
        return callable_()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _invalid("timeout")
    if threading.current_thread() is not threading.main_thread():
        # Python cannot safely interrupt an arbitrary callable from a worker
        # thread.  Refuse the unbounded path rather than leaking a daemon
        # thread that can retain resolver capacity indefinitely.
        raise _invalid("timeout")

    class _DeadlineExpired(Exception):
        pass

    def expire(_signum: int, _frame: Any) -> None:
        raise _DeadlineExpired()

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        return callable_()
    except _DeadlineExpired as exc:
        del exc
        raise _invalid("timeout")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            elapsed = max(0.0, remaining - (deadline - time.monotonic()))
            restored = max(0.0, previous_timer[0] - elapsed)
            signal.setitimer(signal.ITIMER_REAL, restored, previous_timer[1])


def _resolved_addresses(
    resolver: Callable[[str], Iterable[Any]],
    hostname: str,
    *,
    deadline: float | None = None,
) -> list[str]:
    def resolve_and_normalize() -> list[str]:
        try:
            values = resolver(hostname)
        except SourceIngestError:
            raise
        except socket.gaierror as exc:
            del exc
            raise _invalid("dns_error")
        except (OSError, ValueError, TypeError) as exc:
            del exc
            raise _invalid("dns_error")

        addresses: list[str] = []
        try:
            iterator = iter(values)
            for value in iterator:
                if isinstance(value, str):
                    address = value
                elif isinstance(value, (tuple, list)):
                    if value and isinstance(value[0], str):
                        sockaddr: Any = value[4] if len(value) > 4 else None
                        if isinstance(sockaddr, (tuple, list)) and len(sockaddr) > 0:
                            address = sockaddr[0]
                        else:
                            address = value[0]
                    elif len(value) > 4:
                        sockaddr = value[4]
                        if isinstance(sockaddr, (tuple, list)) and len(sockaddr) > 0 and isinstance(sockaddr[0], str):
                            address = sockaddr[0]
                        else:
                            continue
                    else:
                        continue
                else:
                    continue
                if address not in addresses:
                    addresses.append(address)
        except (OSError, ValueError, TypeError) as exc:
            del exc
            raise _invalid("dns_error")
        if not addresses:
            raise _invalid("dns_error")
        return addresses

    return _call_with_deadline(resolve_and_normalize, deadline)


def _validate_host(
    hostname: str,
    resolver: Callable[[str], Iterable[Any]],
    *,
    deadline: float | None = None,
) -> None:
    lowered = hostname.lower().rstrip(".")
    if not lowered or lowered == "localhost" or lowered.endswith(".localhost"):
        raise _invalid("blocked_address")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if deadline is not None and time.monotonic() >= deadline:
            raise _invalid("timeout")
        if not _address_is_public(hostname):
            raise _invalid("blocked_address")
        return

    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in hostname):
        raise _invalid("invalid_url")
    if len(hostname) > 253 or hostname.startswith(".") or hostname.endswith("."):
        raise _invalid("invalid_url")
    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        del exc
        raise _invalid("invalid_url")
    if len(ascii_host) > 253:
        raise _invalid("invalid_url")
    labels = ascii_host.split(".")
    if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in labels):
        raise _invalid("invalid_url")
    if any(not re.fullmatch(r"[A-Za-z0-9-]+", label) for label in labels):
        raise _invalid("invalid_url")
    if not any(character.isalpha() for character in ascii_host):
        raise _invalid("invalid_url")
    if re.fullmatch(r"(?:0[xX][0-9A-Fa-f]+|0[0-7]+|[0-9]+)", ascii_host):
        raise _invalid("invalid_url")
    if not all(_address_is_public(address) for address in _resolved_addresses(resolver, hostname, deadline=deadline)):
        raise _invalid("blocked_address")


def validate_url(
    value: str,
    *,
    resolver: Callable[[str], Iterable[Any]] = resolve_hostname,
    deadline: float | None = None,
) -> SplitResult:
    """Validate syntax and resolve the host, refusing non-public destinations."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise _invalid("invalid_url")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise _invalid("invalid_url")
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise _invalid("invalid_url")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        del exc
        raise _invalid("invalid_url")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not hostname:
        raise _invalid("invalid_url")
    if parsed.username is not None or parsed.password is not None or "#" in value:
        raise _invalid("invalid_url")
    if parsed.netloc.endswith(":"):
        raise _invalid("invalid_url")
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        raise _invalid("invalid_url")
    _validate_host(hostname, resolver, deadline=deadline)
    return parsed


def validate_url_syntax(value: str) -> SplitResult:
    """Validate URL syntax without DNS/network access (the API boundary)."""
    return validate_url(value, resolver=lambda _hostname: ["93.184.216.34"])


class _NoRedirectHandler(HTTPRedirectHandler):
    def http_error_301(self, req, fp, code, msg, headers):
        return fp

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


class _DocumentParser(HTMLParser):
    def __init__(self, max_text_chars: int):
        super().__init__(convert_charrefs=True)
        self.max_text_chars = max(1, int(max_text_chars))
        self.title_parts: list[str] = []
        self.meta_description = ""
        self.headings: list[str] = []
        self.paragraphs: list[str] = []
        self._current_tag: str | None = None
        self._current_parts: list[str] = []
        self._skip_depth = 0
        self._total_text = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta" and not self.meta_description:
            name = attributes.get("name", "").lower()
            if name == "description" and attributes.get("content"):
                self.meta_description = _clean_text(attributes["content"])[: self.max_text_chars]
        if tag == "title" or re.fullmatch(r"h[1-6]", tag) or tag == "p":
            self._current_tag = tag
            self._current_parts = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "meta":
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth or tag != self._current_tag:
            return
        text = _clean_text(" ".join(self._current_parts))
        self._current_tag = None
        self._current_parts = []
        if not text:
            return
        remaining = max(0, self.max_text_chars - self._total_text)
        text = text[:remaining]
        self._total_text += len(text)
        if tag == "title" and not self.title_parts:
            self.title_parts.append(text)
        elif re.fullmatch(r"h[1-6]", tag) and len(self.headings) < MAX_HEADINGS:
            self.headings.append(text[: self.max_text_chars])
        elif tag == "p" and len(self.paragraphs) < MAX_PARAGRAPHS:
            self.paragraphs.append(text[: self.max_text_chars])

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._current_tag is None or self._total_text >= self.max_text_chars:
            return
        self._current_parts.append(data)


def _clean_text(value: str) -> str:
    return " ".join(html.unescape(value).split())


def extract_document(content: str, *, max_text_chars: int = MAX_TEXT_CHARS) -> dict[str, Any]:
    """Extract bounded, deterministic human-readable document fields."""
    if not isinstance(content, str):
        raise _invalid("extraction_error")
    try:
        parser = _DocumentParser(max_text_chars)
        parser.feed(content)
        parser.close()
        paragraphs: list[str] = []
        total = 0
        for paragraph in parser.paragraphs:
            remaining = max(0, parser.max_text_chars - total)
            if not remaining:
                break
            bounded = paragraph[:remaining]
            paragraphs.append(bounded)
            total += len(bounded)
        text = "\n\n".join(paragraphs)[: parser.max_text_chars]
        return {
            "title": _clean_text(" ".join(parser.title_parts))[: parser.max_text_chars],
            "meta_description": parser.meta_description[: parser.max_text_chars],
            "headings": [heading[: parser.max_text_chars] for heading in parser.headings],
            "paragraphs": paragraphs,
            "text": text,
        }
    except SourceIngestError:
        raise
    except (TypeError, ValueError, UnicodeError) as exc:
        del exc
        raise _invalid("extraction_error")


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    if isinstance(status, bool) or not isinstance(status, int) or status < 100 or status > 599:
        raise _invalid("malformed_response")
    return status


def _header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get(name)
            if value is not None:
                return str(value)
        except (AttributeError, TypeError):
            pass
    if hasattr(response, "getheader"):
        value = response.getheader(name)
        return str(value) if value is not None else ""
    return ""


def _read_response(
    response: Any,
    *,
    max_body_bytes: int,
    chunk_size: int,
    deadline: float | None = None,
) -> bytes:
    if max_body_bytes < 1 or chunk_size < 1:
        raise _invalid("body_too_large")
    chunks: list[bytes] = []
    total = 0
    while True:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _invalid("timeout")
            raw = getattr(getattr(response, "fp", None), "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None:
                try:
                    sock.settimeout(remaining)
                except OSError as exc:
                    del exc
                    raise _invalid("network_error")
        try:
            chunk = response.read(min(chunk_size, max_body_bytes + 1 - total))
        except TimeoutError as exc:
            del exc
            raise _invalid("timeout")
        except (OSError, ValueError) as exc:
            del exc
            raise _invalid("network_error")
        except http.client.IncompleteRead as exc:
            del exc
            raise _invalid("malformed_response")
        if not isinstance(chunk, (bytes, bytearray)):
            raise _invalid("malformed_response")
        if not chunk:
            break
        total += len(chunk)
        if total > max_body_bytes:
            raise _invalid("body_too_large")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def _decode_response(body: bytes, content_type: str) -> str:
    match = re.search(r"(?i)(?:^|;)\s*charset=\s*[\"']?([A-Za-z0-9._-]+)", content_type)
    encoding = match.group(1) if match else "utf-8"
    try:
        return body.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        del exc
        raise _invalid("malformed_response")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: float):
        super().__init__(hostname, port=port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: float):
        super().__init__(hostname, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_address, self.port), self.timeout)
        try:
            context = getattr(self, "_context", None)
            if not isinstance(context, ssl.SSLContext):
                context = ssl.create_default_context()
            server_hostname = getattr(self, "_tunnel_host", None) or self.host
            self.sock = context.wrap_socket(self.sock, server_hostname=server_hostname)
        except BaseException:
            self.sock.close()
            self.sock = None
            raise


def _open_response(
    opener: Any | None,
    url: str,
    timeout: float,
    *,
    resolver: Callable[[str], Iterable[Any]],
    deadline: float | None = None,
) -> Any:
    # Test seams may provide an opener. Production traffic uses the checked
    # address directly so a DNS answer cannot be silently replaced by a second
    # hostname lookup inside urllib.
    if opener is not None:
        if isinstance(opener, OpenerDirector):
            raise _invalid("redirect_rejected")
        request = Request(url, headers={"User-Agent": "SoloStudioSourceIngest/1"}, method="GET")
        try:
            return opener.open(request, timeout=timeout)
        except HTTPError as exc:
            return exc
        except TimeoutError as exc:
            del exc
            raise _invalid("timeout")
        except (URLError, OSError) as exc:
            del exc
            raise _invalid("network_error")

    parsed = validate_url(url, resolver=resolver, deadline=deadline)
    addresses = _resolved_addresses(resolver, parsed.hostname or "", deadline=deadline)
    if not addresses or not all(_address_is_public(address) for address in addresses):
        raise _invalid("blocked_address")
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    last_error: SourceIngestError | None = None
    for address in addresses:
        connection: http.client.HTTPConnection | None = None
        last_error = None
        attempt_timeout = timeout
        if deadline is not None:
            attempt_timeout = deadline - time.monotonic()
            if attempt_timeout <= 0:
                raise _invalid("timeout")
        try:
            if parsed.scheme.lower() == "https":
                connection = _PinnedHTTPSConnection(host, address, port, attempt_timeout)
            else:
                connection = _PinnedHTTPConnection(host, address, port, attempt_timeout)
            connection.request("GET", target, headers={"User-Agent": "SoloStudioSourceIngest/1", "Accept": "text/html,text/plain"})
            return connection.getresponse()
        except (TimeoutError, socket.timeout) as exc:
            del exc
            last_error = _invalid("timeout")
        except (OSError, http.client.HTTPException) as exc:
            del exc
            last_error = _invalid("network_error")
        finally:
            if last_error is not None and connection is not None:
                connection.close()
    raise last_error or _invalid("network_error")


def _fetch_one(
    url: str,
    *,
    opener: Any | None,
    resolver: Callable[[str], Iterable[Any]],
    timeout: float,
    max_body_bytes: int,
    max_redirects: int,
    chunk_size: int,
    deadline: float | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout if deadline is None else deadline
    current = url
    redirects: list[str] = []
    for redirect_count in range(max_redirects + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _invalid("timeout")
        validate_url(current, resolver=resolver, deadline=deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _invalid("timeout")
        response = _open_response(opener, current, remaining, resolver=resolver, deadline=deadline)
        try:
            status = _response_status(response)
            if status in {301, 302, 303, 307, 308}:
                location = _header(response, "Location").strip()
                if not location:
                    raise _invalid("malformed_response")
                if redirect_count >= max_redirects:
                    raise _invalid("redirect_limit")
                target = urljoin(current, location)
                try:
                    validate_url(target, resolver=resolver, deadline=deadline)
                except SourceIngestError as exc:
                    if exc.code in {"invalid_url", "blocked_address", "dns_error"}:
                        raise _invalid("blocked_address" if exc.code == "blocked_address" else "redirect_rejected")
                    raise
                redirects.append(target)
                current = target
                continue
            if status < 200 or status >= 300:
                raise _invalid("http_error")
            content_type = _header(response, "Content-Type").split(";", 1)[0].strip().lower()
            if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                raise _invalid("unsupported_media")
            body = _read_response(
                response,
                max_body_bytes=max_body_bytes,
                chunk_size=chunk_size,
                deadline=deadline,
            )
            text = _decode_response(body, _header(response, "Content-Type"))
            extracted = _call_with_deadline(lambda: extract_document(text), deadline)
            extracted.update(
                {
                    "source_url": url,
                    "final_url": current,
                    "redirects": redirects,
                    "content_sha256": hashlib.sha256(body).hexdigest(),
                }
            )
            return extracted
        finally:
            close = getattr(response, "close", None)
            primary_error = sys.exc_info()[0] is not None
            if close is not None:
                try:
                    # HTTPResponse.close() is an owning-thread operation.  A
                    # temporary owner-thread alarm bounds a blocked close
                    # without leaving a daemon cleanup thread behind.
                    _close_response_with_deadline(response, deadline)
                except _ResponseCloseDeadline as exc:
                    if not primary_error:
                        del exc
                        raise _invalid("timeout")
                except SourceIngestError:
                    if not primary_error:
                        raise
                except Exception as exc:
                    if not primary_error:
                        del exc
                        raise _invalid("network_error")
            if not primary_error and time.monotonic() >= deadline:
                raise _invalid("timeout")
    raise _invalid("redirect_limit")


class _ResponseCloseDeadline(Exception):
    pass


def _close_response_with_deadline(response: Any, deadline: float) -> None:
    close = getattr(response, "close", None)
    if close is None:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        remaining = 0.001
    if threading.current_thread() is not threading.main_thread():
        close()
        return
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0.0)

    def interrupt(_signum: int, _frame: Any) -> None:
        raise _ResponseCloseDeadline()

    signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        close()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            elapsed = max(0.0, previous_timer[1] - remaining)
            signal.setitimer(signal.ITIMER_REAL, max(0.001, elapsed), previous_timer[1])



def _safe_word_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", text.lower())


def build_reverse_brief(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Build transparent heuristic fields without an LLM or provider call."""
    if not documents:
        raise _invalid("extraction_error")
    title = next((doc.get("title", "") for doc in documents if doc.get("title")), "the source material")
    descriptions = [doc.get("meta_description", "") for doc in documents if doc.get("meta_description")]
    paragraphs = [paragraph for doc in documents for paragraph in doc.get("paragraphs", []) if paragraph]
    headings = [heading for doc in documents for heading in doc.get("headings", []) if heading]
    value = (descriptions[0] if descriptions else (paragraphs[0] if paragraphs else title))[:500]
    proof_points = (headings[:5] or paragraphs[:5])
    hooks = [f"What {title} gets right", f"The practical lesson behind {title}"]
    objections = []
    for paragraph in paragraphs:
        if re.search(r"\b(but|however|risk|cost|concern|challenge|limit)\b", paragraph, re.I):
            objections.append(paragraph[:240])
        if len(objections) >= 3:
            break
    if not objections:
        objections = ["What are the trade-offs, limitations, or implementation costs?"]
    lower = " ".join(paragraphs + headings).lower()
    if any(word in lower for word in ("cinematic", "film", "story", "documentary")):
        visual_tone = "cinematic, editorial, and story-led"
    elif any(word in lower for word in ("modern", "digital", "software", "technology")):
        visual_tone = "modern, clean, and technology-forward"
    else:
        visual_tone = "clean, credible, and editorial"
    stopwords = {
        "the", "and", "for", "that", "with", "this", "from", "your", "are", "you", "into", "have", "about",
        "their", "will", "more", "what", "how", "our", "can", "not", "but", "all", "they", "its",
    }
    counts = Counter(token for token in _safe_word_tokens(" ".join([title] + headings + paragraphs)) if token not in stopwords)
    vocabulary = [word for word, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:12]]
    audience = "People interested in " + (vocabulary[0] if vocabulary else "this topic")
    angle = f"A concise, evidence-led explainer: {title}."
    return {
        "heuristic": True,
        "target_audience": audience[:240],
        "value_proposition": value,
        "proof_points": proof_points,
        "hooks": hooks,
        "objections": objections,
        "cta": "Learn more and take the next step.",
        "visual_tone": visual_tone,
        "brand_vocabulary": vocabulary,
        "suggested_video_angle": angle[:500],
    }


def _source_context(documents: list[dict[str, Any]], reverse_brief: dict[str, Any]) -> str:
    chunks = ["# Source context", "", "This context is extracted heuristically from the supplied reference pages.", ""]
    for index, doc in enumerate(documents, 1):
        chunks.extend([f"## Source {index}: {doc['title'] or 'Untitled'}", f"Final URL: {doc['final_url']}"])
        if doc.get("meta_description"):
            chunks.append(f"Description: {doc['meta_description']}")
        if doc.get("headings"):
            chunks.append("Headings: " + "; ".join(doc["headings"][:10]))
        if doc.get("text"):
            chunks.extend(["", doc["text"]])
        chunks.append("")
    chunks.extend(
        [
            "## Heuristic reverse brief",
            f"Target audience: {reverse_brief['target_audience']}",
            f"Value proposition: {reverse_brief['value_proposition']}",
            "Proof points: " + "; ".join(reverse_brief["proof_points"]),
            "Hooks: " + "; ".join(reverse_brief["hooks"]),
            "Objections: " + "; ".join(reverse_brief["objections"]),
            f"CTA: {reverse_brief['cta']}",
            f"Visual tone: {reverse_brief['visual_tone']}",
            "Brand vocabulary: " + ", ".join(reverse_brief["brand_vocabulary"]),
            f"Suggested video angle: {reverse_brief['suggested_video_angle']}",
        ]
    )
    return "\n".join(chunks)[: MAX_TEXT_CHARS * 3]


def _assert_no_symlink_ancestors(path: Path) -> None:
    """Reject output paths that traverse an existing symlink or non-directory."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # The remaining path will be created below; there cannot be an
            # existing descendant beneath a missing component.
            break
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise _invalid("write_error")


def _open_bound_output_directory(path: Path) -> int:
    """Open and pin the validated output directory inode without following links."""
    try:
        expected = os.lstat(path)
        if not stat.S_ISDIR(expected.st_mode):
            raise _invalid("write_error")
        descriptor = _open_directory_no_follow(path, create=False)
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            os.close(descriptor)
            raise _invalid("write_error")
        return descriptor
    except SourceIngestError:
        raise
    except (OSError, TypeError) as exc:
        del exc
        raise _invalid("write_error")


def _assert_bound_output_directory(root_fd: int, resolved_root: Path) -> None:
    try:
        current_root = os.lstat(resolved_root)
        pinned_root = os.fstat(root_fd)
    except OSError as exc:
        del exc
        raise _invalid("write_error")
    if (current_root.st_dev, current_root.st_ino) != (pinned_root.st_dev, pinned_root.st_ino):
        raise _invalid("write_error")


def ingest_sources(
    reference_urls: list[str],
    output_dir: str | Path,
    *,
    opener: Any | None = None,
    resolver: Callable[[str], Iterable[Any]] = resolve_hostname,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """Fetch, extract, and atomically publish source artifacts."""
    if threading.current_thread() is not threading.main_thread():
        raise _invalid("network_error")
    if not isinstance(reference_urls, list) or not reference_urls or len(reference_urls) > MAX_REFERENCE_URLS:
        raise _invalid("invalid_url")
    try:
        timeout_value = float(timeout)
    except (OverflowError, TypeError, ValueError):
        raise _invalid("timeout")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout_value)
        or not 0 < timeout_value <= MAX_TIMEOUT_SECONDS
    ):
        raise _invalid("timeout")
    deadline = time.monotonic() + timeout_value
    if (
        isinstance(max_body_bytes, bool)
        or not isinstance(max_body_bytes, int)
        or not 1 <= max_body_bytes <= DEFAULT_MAX_BODY_BYTES
    ):
        raise _invalid("body_too_large")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or not 1 <= chunk_size <= DEFAULT_MAX_BODY_BYTES:
        raise _invalid("body_too_large")
    if isinstance(max_redirects, bool) or not isinstance(max_redirects, int) or not 0 <= max_redirects <= DEFAULT_MAX_REDIRECTS:
        raise _invalid("redirect_limit")
    root_fd = -1
    try:
        root = Path(output_dir)
        _assert_no_symlink_ancestors(root)
        expected_root = None
        try:
            expected_root = os.lstat(root)
            if stat.S_ISLNK(expected_root.st_mode) or not stat.S_ISDIR(expected_root.st_mode):
                raise _invalid("write_error")
        except FileNotFoundError:
            pass
        root_fd = _open_directory_no_follow(root, create=True)
        if expected_root is not None:
            actual_root = os.fstat(root_fd)
            if (actual_root.st_dev, actual_root.st_ino) != (expected_root.st_dev, expected_root.st_ino):
                raise _invalid("write_error")
        resolved_root = root.resolve()
        try:
            if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
                raise _invalid("write_error")
        except OSError as exc:
            del exc
            raise _invalid("write_error")
    except SourceIngestError:
        if root_fd >= 0:
            os.close(root_fd)
            root_fd = -1
        raise
    except (OSError, RuntimeError, TypeError) as exc:
        if root_fd >= 0:
            os.close(root_fd)
            root_fd = -1
        del exc
        raise _invalid("write_error")

    documents: list[dict[str, Any]] = []
    published_entries: list[tuple[str, tuple[object, ...]]] = []
    artifact_names = ("source_manifest.json", "source_context.md", "reverse_brief.json")
    invalid_marker_name = ".source-artifact.invalid"
    artifact_digests: dict[str, str] = {}

    def mark_source_artifacts_invalid() -> BaseException | None:
        marker_fd = -1
        try:
            try:
                marker_fd = os.open(
                    invalid_marker_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=root_fd,
                )
            except FileExistsError:
                return None
            os.write(marker_fd, b"source artifacts invalid; refresh required\\n")
            os.fsync(marker_fd)
            os.fsync(root_fd)
            return None
        except (OSError, TimeoutError) as exc:
            return exc
        finally:
            if marker_fd >= 0:
                try:
                    os.close(marker_fd)
                except OSError:
                    pass

    try:
        try:
            marker_identity = _entry_cleanup_identity_at(root_fd, invalid_marker_name)
        except FileNotFoundError:
            pass
        else:
            _remove_entry_at(root_fd, invalid_marker_name, marker_identity, deadline=deadline)
    except (OSError, TimeoutError) as exc:
        os.close(root_fd)
        root_fd = -1
        del exc
        raise _invalid("write_error")

    def quarantine_current_entry(name: str) -> None:
        quarantine_name = f".source-artifact.quarantine-{os.getpid()}-{time.monotonic_ns()}"
        try:
            _rename_noreplace(root_fd, name, root_fd, quarantine_name)
        except FileNotFoundError:
            return
        os.fsync(root_fd)

    def cleanup_published_entries() -> BaseException | None:
        cleanup_deadline = max(deadline, time.monotonic() + CLEANUP_GRACE_SECONDS)
        cleanup_error: BaseException | None = None
        entries = list(published_entries)
        tracked = {name for name, _identity in entries}
        for name in artifact_names:
            if name in tracked:
                continue
            try:
                _contain_entry_at(
                    root_fd,
                    name,
                    ("untrusted-source-entry", os.getpid(), name),
                    "source-artifact",
                    deadline=cleanup_deadline,
                )
            except FileNotFoundError:
                pass
            except (OSError, TimeoutError) as exc:
                try:
                    quarantine_current_entry(name)
                except (OSError, TimeoutError):
                    cleanup_error = cleanup_error or RuntimeError("source artifact cleanup uncertain")
                del exc
        for name, identity in reversed(entries):
            try:
                _remove_entry_at(
                    root_fd,
                    name,
                    identity,
                    deadline=cleanup_deadline,
                )
            except FileNotFoundError:
                pass
            except (OSError, TimeoutError) as primary_cleanup:
                try:
                    _contain_entry_at(
                        root_fd,
                        name,
                        identity,
                        "source-artifact",
                        deadline=cleanup_deadline,
                    )
                except FileNotFoundError:
                    pass
                except (OSError, TimeoutError) as containment:
                    try:
                        quarantine_current_entry(name)
                    except (OSError, TimeoutError):
                        cleanup_error = cleanup_error or RuntimeError("source artifact cleanup uncertain")
                    del containment
                del primary_cleanup
        if cleanup_error is not None:
            marker_error = mark_source_artifacts_invalid()
            if marker_error is not None:
                cleanup_error = RuntimeError("source artifact invalidation marker could not be written")
                cleanup_error.__cause__ = marker_error
        return cleanup_error
    try:
        for name in artifact_names:
            try:
                prior_artifact = os.lstat(name, dir_fd=root_fd)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(prior_artifact.st_mode):
                raise _invalid("write_error")
            _remove_entry_at(
                root_fd,
                name,
                _cleanup_identity(prior_artifact),
                deadline=max(deadline, time.monotonic() + CLEANUP_GRACE_SECONDS),
            )
        for url in reference_urls:
            if time.monotonic() >= deadline:
                raise _invalid("timeout")
            document = _fetch_one(
                url,
                opener=opener,
                resolver=resolver,
                timeout=float(timeout),
                max_body_bytes=max_body_bytes,
                max_redirects=max_redirects,
                chunk_size=chunk_size,
                deadline=deadline,
            )
            documents.append(document)
        reverse_brief = _call_with_deadline(lambda: build_reverse_brief(documents), deadline)
        context = _call_with_deadline(lambda: _source_context(documents, reverse_brief), deadline)
        reverse_bytes = json.dumps(reverse_brief, indent=2, allow_nan=False).encode("utf-8")
        manifest = {
            "version": 1,
            "heuristic": True,
            "artifacts": {
                "source_context.md": hashlib.sha256(context.encode("utf-8")).hexdigest(),
                "reverse_brief.json": hashlib.sha256(reverse_bytes).hexdigest(),
            },
            "sources": [
                {
                    "source_url": doc["source_url"],
                    "final_url": doc["final_url"],
                    "redirects": doc["redirects"],
                    "title": doc["title"],
                    "meta_description": doc["meta_description"],
                    "headings": doc["headings"],
                    "content_sha256": doc["content_sha256"],
                    "status": "ok",
                }
                for doc in documents
            ],
        }
        manifest_path = resolved_root / "source_manifest.json"
        context_path = resolved_root / "source_context.md"
        reverse_path = resolved_root / "reverse_brief.json"
        artifact_digests = {
            "source_manifest.json": hashlib.sha256(json.dumps(manifest, indent=2, allow_nan=False).encode("utf-8")).hexdigest(),
            "source_context.md": hashlib.sha256(context.encode("utf-8")).hexdigest(),
            "reverse_brief.json": hashlib.sha256(json.dumps(reverse_brief, indent=2, allow_nan=False).encode("utf-8")).hexdigest(),
        }
        for path in (manifest_path, context_path, reverse_path):
            if path.parent != resolved_root:
                raise _invalid("write_error")
        _assert_bound_output_directory(root_fd, resolved_root)
        context_identity = atomic_write_text(context_path, context, deadline=deadline, _directory_fd=root_fd)
        if context_identity is None:
            raise _invalid("write_error")
        published_entries.append((context_path.name, context_identity))
        _assert_bound_output_directory(root_fd, resolved_root)
        reverse_identity = atomic_write_json(reverse_path, reverse_brief, deadline=deadline, _directory_fd=root_fd)
        if reverse_identity is None:
            raise _invalid("write_error")
        published_entries.append((reverse_path.name, reverse_identity))
        _assert_bound_output_directory(root_fd, resolved_root)
        manifest_identity = atomic_write_json(manifest_path, manifest, deadline=deadline, _directory_fd=root_fd)
        if manifest_identity is None:
            raise _invalid("write_error")
        published_entries.append((manifest_path.name, manifest_identity))
        for name, identity in published_entries:
            expected_digest = artifact_digests.get(name)
            try:
                current_identity = _entry_cleanup_identity_at(root_fd, name)
                current_digest = _regular_content_digest_at(root_fd, name, deadline=deadline)
            except (FileNotFoundError, OSError, TimeoutError) as exc:
                try:
                    _contain_entry_at(root_fd, name, identity, "source-artifact", deadline=max(deadline, time.monotonic() + CLEANUP_GRACE_SECONDS))
                except (FileNotFoundError, OSError, TimeoutError) as containment:
                    try:
                        quarantine_current_entry(name)
                    except (FileNotFoundError, OSError, TimeoutError):
                        pass
                    del containment
                raise _invalid("write_error") from exc
            if current_identity != identity or current_digest != expected_digest:
                try:
                    _contain_entry_at(root_fd, name, identity, "source-artifact", deadline=max(deadline, time.monotonic() + CLEANUP_GRACE_SECONDS))
                except (FileNotFoundError, OSError, TimeoutError) as containment:
                    try:
                        quarantine_current_entry(name)
                    except (FileNotFoundError, OSError, TimeoutError):
                        pass
                    del containment
                raise _invalid("write_error")
        try:
            current_root = os.lstat(resolved_root)
            pinned_root = os.fstat(root_fd)
        except OSError as exc:
            del exc
            raise _invalid("write_error")
        if (current_root.st_dev, current_root.st_ino) != (pinned_root.st_dev, pinned_root.st_ino):
            raise _invalid("write_error")
        canonical_fd = -1
        try:
            canonical_fd = _open_directory_no_follow(resolved_root, create=False)
            canonical_root = os.fstat(canonical_fd)
            if (canonical_root.st_dev, canonical_root.st_ino) != (pinned_root.st_dev, pinned_root.st_ino):
                raise _invalid("write_error")
        except SourceIngestError:
            raise
        except OSError as exc:
            del exc
            raise _invalid("write_error")
        finally:
            if canonical_fd >= 0:
                os.close(canonical_fd)
        if time.monotonic() >= deadline:
            raise _invalid("timeout")
        return manifest
    except SourceIngestError as exc:
        cleanup_error = cleanup_published_entries()
        if cleanup_error is not None:
            raise exc.with_traceback(exc.__traceback__) from cleanup_error
        raise
    except TimeoutError as exc:
        cleanup_error = cleanup_published_entries()
        normalized = _invalid("timeout")
        if cleanup_error is not None:
            raise normalized from cleanup_error
        del exc
        raise normalized
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        cleanup_error = cleanup_published_entries()
        normalized = _invalid("write_error")
        if cleanup_error is not None:
            raise normalized from cleanup_error
        del exc
        raise normalized
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _read_bounded_brief(path: Path) -> str:
    descriptor = _open_regular_descriptor(path)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BRIEF_BYTES:
            raise _invalid("malformed_response")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, MAX_BRIEF_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BRIEF_BYTES:
                raise _invalid("malformed_response")
            chunks.append(chunk)
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            del exc
            raise _invalid("malformed_response")
    finally:
        os.close(descriptor)


def main() -> None:
    """CLI entrypoint used by the bounded pipeline stage."""
    if len(sys.argv) != 3:
        print("Usage: python source_ingest_agent.py <brief.yaml> <output_dir>")
        raise SystemExit(2)
    try:
        brief_path = Path(sys.argv[1])
        raw_text = _read_bounded_brief(brief_path)
        brief = yaml.safe_load(raw_text)
        if not isinstance(brief, dict):
            raise _invalid("invalid_url")
        manifest = ingest_sources(brief.get("reference_urls", []), sys.argv[2])
        print(f"Source manifest: {len(manifest['sources'])} source(s)")
    except SourceIngestError as exc:
        print(f"Source ingestion failed: {exc.code}")
        raise SystemExit(1)
    except (OSError, UnicodeError, TypeError, ValueError, yaml.YAMLError):
        print("Source ingestion failed: ingestion_error")
        raise SystemExit(1)
    except Exception:
        print("Source ingestion failed: ingestion_error")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
