"""Generate verified video clips through the configured Higgsfield CLI.

Default behavior remains a safe dry-run. Real generation requires:
- ``higgsfield`` in PATH and authenticated;
- ``SOLO_STUDIO_ENABLE_HIGGSFIELD=1``;
- a provider response containing an HTTPS result URL for every scene.

A real run writes downloaded MP4s under ``clips/`` and returns non-zero when any
scene fails, so the worker cannot mark an editor-only package as fully successful.
"""
from __future__ import annotations

import json
import http.client
import ipaddress
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_MODEL = os.environ.get("SOLO_STUDIO_HIGGSFIELD_MODEL", "seedance_2_0")
TRUTHY = {"1", "true", "yes", "on"}
MAX_CLIP_BYTES = int(os.environ.get("SOLO_STUDIO_MAX_CLIP_BYTES", str(200 * 1024 * 1024)))


def higgsfield_enabled() -> bool:
    """Read the enable flag at runtime so tests/workers can safely override env."""
    return os.environ.get("SOLO_STUDIO_ENABLE_HIGGSFIELD", "").strip().lower() in TRUTHY


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path) as handle:
        return json.load(handle)


def sanitize_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:8000]


def seedance_prompt(scene: dict[str, Any]) -> str:
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
        f"Generate a {duration:g}-second 16:9 video clip for scene {scene.get('scene_number', '?')}.\n"
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
        candidates.extend(payload.get(key) for key in ("result_url", "url", "video_url", "download_url"))
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


def _verify_clip(path: Path) -> tuple[bool, str | None]:
    """Require an MP4 signature and a positive ffprobe duration before publish."""
    if path.stat().st_size < 12:
        return False, "Provider returned an invalid or empty MP4 file."
    with path.open("rb") as handle:
        header = handle.read(12)
    if header[4:8] != b"ftyp":
        return False, "Provider returned a non-MP4 artifact."
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False, "ffprobe is required to verify generated clips."
    try:
        probe = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_type:format=duration",
                "-of", "json",
                str(path),
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "ffprobe could not verify the generated clip."
    if probe.returncode != 0:
        return False, "ffprobe rejected the generated clip."
    try:
        probe_payload = json.loads(probe.stdout)
        streams = probe_payload.get("streams", [])
        if not any(stream.get("codec_type") == "video" for stream in streams if isinstance(stream, dict)):
            return False, "Generated clip contains no video stream."
        duration = float(probe_payload.get("format", {}).get("duration"))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return False, "ffprobe returned no usable clip duration."
    if not math.isfinite(duration) or duration <= 0:
        return False, "Generated clip duration must be positive."
    return True, None


def _public_provider_ip(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Provider artifact URL must use HTTPS.")
    if parsed.port not in (None, 443):
        raise ValueError("Provider artifact URL must use HTTPS on port 443.")
    try:
        addresses = {
            sockaddr[4][0]
            for sockaddr in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise ValueError("Provider artifact host could not be resolved.") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Provider artifact host is not publicly routable.")
    return str(sorted(addresses)[0])


def _assert_safe_provider_url(url: str) -> None:
    _public_provider_ip(url)


class _SafeProviderRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_safe_provider_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, *, pinned_ip: str, **kwargs):
        super().__init__(host, **kwargs)
        self.pinned_ip = pinned_ip

    def connect(self):
        source_address = getattr(self, "source_address", None)
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout, source_address)
        tunnel_host = getattr(self, "_tunnel_host", None)
        if tunnel_host:
            getattr(self, "_tunnel")()
        self.sock = getattr(self, "_context").wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        pinned_ip = _public_provider_ip(req.full_url)
        return self.do_open(
            lambda host, **kwargs: _PinnedHTTPSConnection(host, pinned_ip=pinned_ip, **kwargs),
            req,
        )


def _open_provider_url(url: str):
    _assert_safe_provider_url(url)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _PinnedHTTPSHandler(),
        _SafeProviderRedirectHandler(),
    )
    return opener.open(url, timeout=120)


def run_higgsfield(prompt: str, duration: float, out_file: Path, model: str) -> dict[str, Any]:
    command = [
        "higgsfield", "generate", "create", model,
        "--prompt", prompt,
        "--aspect_ratio", "16:9",
        "--duration", str(int(round(duration))),
        "--resolution", os.environ.get("SOLO_STUDIO_HIGGSFIELD_RESOLUTION", "720p"),
        "--wait", "--json",
    ]
    try:
        process = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=int(os.environ.get("SOLO_STUDIO_HIGGSFIELD_TIMEOUT", "900")),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": "Higgsfield CLI timed out. Check provider logs on the host."}
    except OSError as exc:
        return {"status": "failed", "error": f"Higgsfield CLI could not start: {exc.__class__.__name__}"}

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
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        result["status"] = "failed"
        result["error"] = "Higgsfield CLI returned non-JSON output; refusing to infer a video URL."
        return result
    if not _provider_status_is_usable(payload):
        result["status"] = "failed"
        result["error"] = "Provider did not report a completed generation."
        return result
    result_url = _provider_url(payload)
    if not result_url:
        result["status"] = "failed"
        result["error"] = "Provider completed without an HTTPS video URL."
        return result

    temporary_handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{out_file.name}.",
        suffix=".part",
        dir=out_file.parent,
        delete=False,
    )
    temporary_file = Path(temporary_handle.name)
    try:
        total = 0
        with temporary_handle as handle, _open_provider_url(result_url) as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_CLIP_BYTES:
                    raise ValueError("Provider artifact exceeds the configured size limit.")
                handle.write(chunk)
        if total == 0:
            raise ValueError("Provider returned an empty video file.")
        valid, verification_error = _verify_clip(temporary_file)
        if not valid:
            result["status"] = "failed"
            result["error"] = verification_error
            return result
        temporary_file.replace(out_file)
    except Exception:
        result["status"] = "failed"
        result["error"] = "Video download failed; check provider logs on the host."
        return result
    finally:
        temporary_file.unlink(missing_ok=True)

    result.update({"status": "downloaded", "output_file": str(out_file), "bytes": out_file.stat().st_size, "duration_verified": True})
    return result


def generate_plan(video_prompts_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    clips_dir = output_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    try:
        prompts = load_json(video_prompts_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        prompts = None
    model = os.environ.get("SOLO_STUDIO_HIGGSFIELD_MODEL", DEFAULT_MODEL)
    binary = shutil.which("higgsfield")
    enable_higgsfield = higgsfield_enabled()
    real_mode = enable_higgsfield and bool(binary)
    plan: dict[str, Any] = {
        "status": "generating" if real_mode else ("setup_needed" if enable_higgsfield else "dry_run"),
        "backend": "higgsfield",
        "model": model,
        "enabled": real_mode,
        "higgsfield_binary": binary,
        "reason": None if real_mode else (
            "Set SOLO_STUDIO_ENABLE_HIGGSFIELD=1 and install/authenticate `higgsfield` CLI to generate real clips."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scenes": [],
    }
    plan["setup_needed"] = plan["reason"]
    if not isinstance(prompts, dict):
        plan["status"] = "failed"
        plan["reason"] = "Video prompts must be a JSON object with a scenes list."
        plan["setup_needed"] = plan["reason"]
        plan["total_scenes"] = 0
        (clips_dir / "generation_plan.json").write_text(json.dumps(plan, indent=2))
        return plan
    scenes = prompts.get("scenes", [])
    if not isinstance(scenes, list) or not scenes:
        plan["status"] = "failed"
        plan["reason"] = "No scenes were available: video prompts must contain a non-empty scenes list."
        plan["setup_needed"] = plan["reason"]
        plan["total_scenes"] = 0
        (clips_dir / "generation_plan.json").write_text(json.dumps(plan, indent=2))
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
        (clips_dir / "generation_plan.json").write_text(json.dumps(plan, indent=2))
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
        (clips_dir / "generation_plan.json").write_text(json.dumps(plan, indent=2))
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
        (clips_dir / "generation_plan.json").write_text(json.dumps(plan, indent=2))
        return plan
    for stale_clip in clips_dir.glob("scene_*.mp4"):
        try:
            stale_clip.unlink()
        except OSError:
            pass
    all_downloaded = real_mode and bool(scenes)
    if real_mode and not scenes:
        plan["status"] = "failed"
        plan["reason"] = "No scenes were available for provider generation."
        plan["setup_needed"] = plan["reason"]
        plan["total_scenes"] = 0
        plan_path = clips_dir / "generation_plan.json"
        plan_path.write_text(json.dumps(plan, indent=2))
        return plan
    for scene, duration in zip(scenes, durations):
        number = int(scene.get("scene_number", len(plan["scenes"]) + 1))
        item: dict[str, Any] = {
            "scene_number": number,
            "duration_seconds": duration,
            "model": model,
            "prompt": scene.get("seedance_prompt") or seedance_prompt(scene),
            "target_file": f"clips/scene_{number:02d}.mp4",
            "status": "pending" if real_mode else "dry_run",
            "source_prompts": {
                "seedance": scene.get("seedance_prompt", ""),
                "runway": scene.get("runway_prompt", ""),
                "pika": scene.get("pika_prompt", ""),
                "kling": scene.get("kling_prompt", ""),
            },
        }
        if real_mode:
            item.update(run_higgsfield(item["prompt"], duration, clips_dir / f"scene_{number:02d}.mp4", model))
            all_downloaded = all_downloaded and item["status"] == "downloaded"
        plan["scenes"].append(item)
    if real_mode:
        plan["status"] = "completed" if all_downloaded else "failed"
        plan["reason"] = None if all_downloaded else "One or more provider clips failed or were not downloaded."
        plan["setup_needed"] = plan["reason"]
    plan["total_scenes"] = len(plan["scenes"])
    plan_path = clips_dir / "generation_plan.json"
    plan_path.write_text(json.dumps(plan, indent=2))
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
    print(f"Mode: {plan['status']} | scenes={len(plan['scenes'])} | model={plan['model']}")
    if plan.get("reason"):
        print(f"Reason: {plan['reason']}")
    return 0 if plan["status"] == "dry_run" or complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
