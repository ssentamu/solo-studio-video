"""Explicitly gated provider canary for one real video/audio run.

Default invocation is a no-network dry run. Live mode requires both
``--live --confirm-spend`` and ``SOLO_STUDIO_PROVIDER_CANARY_LIVE=1``. Results
are deliberately reduced to safe metadata; provider URLs, stdout, credentials,
and local artifact paths never reach the JSON report.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import multiprocessing
import os
import signal
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
_SUPERVISION_OWNERS: dict[str, tuple[int, int]] = {}
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audio_generation import AudioGenerationError, generate_voiceover
from engines.generation_agent import run_higgsfield
from media_assembly import MediaError
from music_generation import MusicGenerationError, generate_music


VIDEO_PROMPT = "A clean cinematic close-up of a glowing developer workstation, slow camera push-in, no text."
VOICEOVER_TEXT = "This is a short provider canary for voice generation."
MUSIC_PROMPT = "Short instrumental cinematic technology bed, subtle pulse, no vocals, clean ending."
CANARY_TOTAL_TIMEOUT_SECONDS = 180.0


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_result(provider: str, status: str, metadata: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"provider": provider, "status": status}
    if metadata:
        for key in ("bytes", "duration_seconds", "duration_verified", "audio_verified", "sha256"):
            if key in metadata:
                result[key] = metadata[key]
    if error:
        result["error_type"] = error
    return result


def _enable_child_subreaper() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        if prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            return False
        return True
    except (AttributeError, OSError):
        return False


def _proc_observation(pid: int) -> tuple[str, tuple[int, int, str] | None]:
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


def _proc_identity(pid: int) -> tuple[int, int, str] | None:
    status, identity = _proc_observation(pid)
    return identity if status == "present" else None


def _direct_child_identities_state(parent_pid: int) -> tuple[dict[int, int], bool]:
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
        status, identity = _proc_observation(pid)
        if status == "unknown":
            complete = False
            continue
        if identity is not None and identity[0] == parent_pid:
            children[pid] = identity[1]
    return children, complete


def _direct_child_identities(parent_pid: int) -> dict[int, int]:
    return _direct_child_identities_state(parent_pid)[0]


def _track_adopted_descendants(
    supervisor_pid: int,
    baseline_children: dict[int, int],
    tracked: dict[int, int],
) -> bool:
    """Validate adopted children without claiming unrelated post-baseline children."""
    children, complete = _direct_child_identities_state(supervisor_pid)
    boundary_ok = complete
    for pid, starttime in children.items():
        if baseline_children.get(pid) == starttime:
            continue
        if tracked.get(pid) != starttime:
            boundary_ok = False
    for pid in tuple(tracked):
        boundary_ok = _tracked_process_tree(pid, tracked) and boundary_ok
    return boundary_ok


def _token_owned_processes(token: str, tracked: dict[int, int]) -> bool:
    """Validate the per-run owner binding and every present tracked identity."""
    if len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
        return False
    owner = _SUPERVISION_OWNERS.get(token)
    if owner is None or tracked.get(owner[0]) != owner[1]:
        return False
    for pid, starttime in tracked.items():
        status, identity = _proc_observation(pid)
        if status == "unknown":
            return False
        if status == "gone":
            continue
        if identity is None or identity[1] != starttime:
            return False
    return True


def _tracked_process_tree(root_pid: int, known: dict[int, int]) -> bool:
    frontier = dict(known)
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
            identity = _proc_identity(pid)
            if identity is None or pid in known:
                continue
            parent_starttime = frontier.get(identity[0])
            parent_identity = _proc_identity(identity[0]) if parent_starttime is not None else None
            if parent_identity is None or parent_identity[1] != parent_starttime:
                continue
            known[pid] = identity[1]
            frontier[pid] = identity[1]
            changed = True
        if not changed:
            return True
        if depth == 7:
            return False
    return True

def _kill_tracked_processes(
    root_pid: int,
    baseline_children: dict[int, int],
    tracked_out: dict[int, int] | None = None,
    root_starttime: int | None = None,
    *,
    deadline: float | None = None,
) -> bool:
    tracked = dict(tracked_out or {})
    if root_starttime is None:
        root_starttime = tracked.get(root_pid)
    if root_starttime is None:
        return False
    tracked[root_pid] = root_starttime
    def adopted_boundary_ok() -> bool:
        children, complete = _direct_child_identities_state(os.getpid())
        return complete and all(
            baseline_children.get(pid) == starttime or tracked.get(pid) == starttime
            for pid, starttime in children.items()
        )

    boundary_ok = adopted_boundary_ok()
    root_status, current_root = _proc_observation(root_pid)
    root_matches = root_status == "present" and current_root is not None and current_root[1] == root_starttime
    if root_matches:
        boundary_ok = _tracked_process_tree(root_pid, tracked) and boundary_ok
        try:
            os.kill(root_pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    for _ in range(8):
        boundary_ok = adopted_boundary_ok() and boundary_ok
        expired = deadline is not None and time.monotonic() >= deadline
        if expired:
            if tracked_out is not None:
                tracked_out.update(tracked)
        if root_matches:
            boundary_ok = _tracked_process_tree(root_pid, tracked) and boundary_ok
        alive = False
        for pid, starttime in list(tracked.items()):
            current_status, current = _proc_observation(pid)
            if current_status == "unknown":
                alive = True
                continue
            if current is None or current[1] != starttime:
                continue
            if current[2] == "Z":
                if pid != root_pid:
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except (ChildProcessError, OSError):
                        pass
                continue
            if pid != root_pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            alive = True
        if expired:
            time.sleep(0.01)
            break
        if not alive:
            break
        time.sleep(0.005)
    for pid, starttime in tracked.items():
        if pid == root_pid:
            continue
        for _ in range(8):
            current_status, current = _proc_observation(pid)
            if current_status == "unknown":
                break
            if current is None or current[1] != starttime:
                break
            if current[2] == "Z":
                try:
                    waited_pid, _status = os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    break
                if waited_pid == pid:
                    break
            time.sleep(0.005)
    for pid, starttime in tracked.items():
        current_status, current = _proc_observation(pid)
        if current_status == "unknown":
            if tracked_out is not None:
                tracked_out.update(tracked)
            return False
        if current is not None and current[1] == starttime and current[2] != "Z" and pid != root_pid:
            if tracked_out is not None:
                tracked_out.update(tracked)
            return False
    final_root_status, final_root = _proc_observation(root_pid)
    boundary_ok = adopted_boundary_ok() and boundary_ok
    if root_status == "unknown" or final_root_status == "unknown":
        if tracked_out is not None:
            tracked_out.update(tracked)
        return False
    if root_matches is False and (current_root is not None or final_root is not None):
        if tracked_out is not None:
            tracked_out.update(tracked)
        return False
    if tracked_out is not None:
        tracked_out.update(tracked)
    return boundary_ok


def _reap_adopted_children(tracked: dict[int, int], *, deadline: float | None = None) -> bool:
    for _ in range(16):
        expired = deadline is not None and time.monotonic() >= deadline
        adopted = _direct_child_identities(os.getpid())
        pending = False
        for pid, starttime in adopted.items():
            if tracked.get(pid) != starttime:
                continue
            current_status, current = _proc_observation(pid)
            if current_status == "unknown":
                return False
            if current is None or current[1] != starttime:
                continue
            pending = True
            if current[2] != "Z":
                try:
                    os.kill(pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            else:
                try:
                    waited_pid, _status = os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    waited_pid = 0
                if waited_pid == pid:
                    pending = False
        if not pending:
            return not expired
        if expired:
            return False
        time.sleep(0.005)
    return not any(tracked.get(pid) == starttime for pid, starttime in _direct_child_identities(os.getpid()).items())


def _run_video(root: Path, deadline: float) -> dict[str, Any]:
    destination = root / "video" / "scene_01.mp4"
    destination.parent.mkdir()
    try:
        result = run_higgsfield(
            VIDEO_PROMPT,
            5.0,
            destination,
            "seedance_2_0",
            "16:9",
            expected_width=1920,
            expected_height=1080,
            deadline=deadline,
        )
    except Exception:
        return _safe_result("higgsfield-video", "failed", error="provider_or_media_verification_failed")
    if result.get("status") != "downloaded":
        return _safe_result("higgsfield-video", "failed", error="provider_or_media_verification_failed")
    return _safe_result("higgsfield-video", "passed", result)


def _run_voiceover(root: Path, deadline: float) -> dict[str, Any]:
    expected_endpoint = "https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM"
    endpoint = os.environ.get("SOLO_STUDIO_TTS_ENDPOINT", expected_endpoint).strip()
    allowed_hosts = {
        value.strip().lower()
        for value in os.environ.get("SOLO_STUDIO_TTS_ALLOWED_HOSTS", "api.elevenlabs.io").split(",")
        if value.strip()
    }
    model = os.environ.get("SOLO_STUDIO_TTS_MODEL", "eleven_multilingual_v2").strip()
    voice_id = os.environ.get("SOLO_STUDIO_TTS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM").strip()
    if (
        endpoint != expected_endpoint
        or allowed_hosts != {"api.elevenlabs.io"}
        or model != "eleven_multilingual_v2"
        or voice_id != "21m00Tcm4TlvDq8ikWAM"
    ):
        return _safe_result("elevenlabs-tts", "failed", error="fixed_tts_contract_mismatch")
    try:
        result = generate_voiceover(
            VOICEOVER_TEXT,
            root / "audio" / "voiceover.mp3",
            max_duration_seconds=30.0,
            deadline=deadline,
        )
    except (AudioGenerationError, OSError, MediaError):
        return _safe_result("elevenlabs-tts", "failed", error="provider_or_media_verification_failed")
    except Exception:
        return _safe_result("elevenlabs-tts", "failed", error="provider_or_media_verification_failed")
    return _safe_result("elevenlabs-tts", "passed", result)


def _run_music(root: Path, deadline: float) -> dict[str, Any]:
    destination = root / "music" / "background.mp3"
    destination.parent.mkdir()
    try:
        result = generate_music(MUSIC_PROMPT, 5.0, destination, deadline=deadline)
    except (MusicGenerationError, OSError, MediaError):
        return _safe_result("higgsfield-seed-audio", "failed", error="provider_or_media_verification_failed")
    except Exception:
        return _safe_result("higgsfield-seed-audio", "failed", error="provider_or_media_verification_failed")
    return _safe_result("higgsfield-seed-audio", "passed", result)


def _check_child(check_name: str, root: str, deadline: float, connection: Any) -> None:
    os.setsid()
    if not _enable_child_subreaper():
        connection.send({"provider": "canary", "status": "failed", "error_type": "canary_supervision_unavailable"})
        connection.close()
        return
    checks = {"video": _run_video, "voiceover": _run_voiceover, "music": _run_music}
    try:
        result = checks[check_name](Path(root), deadline)
        connection.send(result if isinstance(result, dict) else {"status": "failed", "error_type": "invalid_check_result"})
        remaining = max(0.0, deadline - time.monotonic())
        if remaining > 0:
            time.sleep(min(0.25, remaining))
    except BaseException:
        connection.send({"status": "failed", "error_type": "provider_or_media_verification_failed"})
    finally:
        connection.close()


def _supervised_check_child(check_name: str, root: str, deadline: float, connection: Any) -> None:
    if not _enable_child_subreaper():
        connection.send({"provider": "canary", "status": "failed", "error_type": "canary_supervision_unavailable"})
        connection.close()
        return
    _check_child(check_name, root, deadline, connection)


def _join_process_with_deadline(process: Any, deadline: float, *, after_force_kill: bool = False) -> None:
    remaining = max(0.0, deadline - time.monotonic())
    if after_force_kill:
        remaining = max(remaining, 0.01)
    process.join(remaining)


def _run_check_bounded(check_name: str, root: Path, deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _safe_result("canary", "failed", error="canary_deadline_exceeded")
    if not _enable_child_subreaper():
        return _safe_result("canary", "failed", error="canary_supervision_unavailable")
    baseline_children, baseline_complete = _direct_child_identities_state(os.getpid())
    if not baseline_complete:
        return _safe_result("canary", "failed", error="canary_supervision_unavailable")
    parent = child = process = None
    process_started = False
    try:
        context = multiprocessing.get_context("fork")
        parent, child = context.Pipe(duplex=False)
        supervision_token = uuid.uuid4().hex
        process = context.Process(target=_supervised_check_child, args=(check_name, str(root), deadline, child))
        previous_token = os.environ.get("SOLO_STUDIO_SUPERVISION_TOKEN")
        process_started = False
        os.environ["SOLO_STUDIO_SUPERVISION_TOKEN"] = supervision_token
        try:
            process.start()
            process_started = True
        finally:
            if previous_token is None:
                os.environ.pop("SOLO_STUDIO_SUPERVISION_TOKEN", None)
            else:
                os.environ["SOLO_STUDIO_SUPERVISION_TOKEN"] = previous_token
    except (OSError, RuntimeError) as exc:
        if process is not None and process_started:
            try:
                process.kill()
                process.join(0)
            except (OSError, RuntimeError, AssertionError):
                pass
        for connection in (child, parent):
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
        return _safe_result("canary", "failed", error="canary_process_start_failed")
    assert process is not None
    assert parent is not None
    assert child is not None
    assert process.pid is not None
    root_identity = _proc_identity(process.pid)
    root_starttime = root_identity[1] if root_identity is not None else None
    if root_starttime is not None:
        _SUPERVISION_OWNERS[supervision_token] = (process.pid, root_starttime)
    child.close()
    result: dict[str, Any]
    try:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0 or not parent.poll(remaining):
            result = _safe_result("canary", "failed", error="canary_deadline_exceeded")
        else:
            try:
                received = parent.recv()
            except (EOFError, OSError):
                received = None
            result = received if isinstance(received, dict) else _safe_result(
                "canary", "failed", error="provider_or_media_verification_failed"
            )
            if time.monotonic() >= deadline:
                result = _safe_result("canary", "failed", error="canary_deadline_exceeded")
    finally:
        parent.close()
        tracked_children: dict[int, int] = {}
        if root_starttime is not None:
            tracked_children[process.pid] = root_starttime

        def teardown_worker(connection: Any) -> None:
            supervision_ok = True
            tracked: dict[int, int] = {}
            def refresh_supervision() -> bool:
                if process.pid is None:
                    return False
                boundary_ok = _track_adopted_descendants(os.getpid(), baseline_children, tracked)
                descendants_ok = _tracked_process_tree(process.pid, tracked)
                if _proc_identity(process.pid) is None:
                    return boundary_ok and descendants_ok
                return boundary_ok and descendants_ok and _token_owned_processes(supervision_token, tracked)

            if root_starttime is not None and process.pid is not None:
                tracked[process.pid] = root_starttime
            try:
                if process.pid is None or root_starttime is None:
                    supervision_ok = False
                else:
                    for _ in range(16):
                        supervision_ok = refresh_supervision() and supervision_ok
                        if time.monotonic() < deadline:
                            time.sleep(0.01)
                        supervision_ok = refresh_supervision() and supervision_ok
                        supervision_ok = _kill_tracked_processes(process.pid, baseline_children, tracked, root_starttime, deadline=deadline) and supervision_ok
                        supervision_ok = _reap_adopted_children(tracked, deadline=deadline) and supervision_ok
                        if time.monotonic() < deadline:
                            time.sleep(0.01)
                    supervision_ok = refresh_supervision() and supervision_ok
                    supervision_ok = _kill_tracked_processes(process.pid, baseline_children, tracked, root_starttime, deadline=deadline) and supervision_ok
                    supervision_ok = _reap_adopted_children(tracked, deadline=deadline) and supervision_ok
                connection.send((supervision_ok, tracked))
            except BaseException:
                try:
                    connection.send((False, tracked))
                except (BrokenPipeError, OSError):
                    pass
            finally:
                connection.close()

        cleanup_parent = cleanup_child = cleanup_process = None
        try:
            cleanup_parent, cleanup_child = context.Pipe(duplex=False)
            cleanup_process = context.Process(target=teardown_worker, args=(cleanup_child,))
            cleanup_process.start()
            cleanup_child.close()
        except BaseException:
            fallback_tracked = dict(tracked_children)
            fallback_cleanup_ok = False
            try:
                if process.pid is not None and root_starttime is not None:
                    fallback_cleanup_ok = _track_adopted_descendants(
                        os.getpid(),
                        baseline_children,
                        fallback_tracked,
                    )
                    fallback_cleanup_ok = _kill_tracked_processes(
                        process.pid,
                        baseline_children,
                        fallback_tracked,
                        root_starttime,
                        deadline=deadline,
                    ) and fallback_cleanup_ok
                    fallback_cleanup_ok = _reap_adopted_children(
                        fallback_tracked,
                        deadline=deadline,
                    ) and fallback_cleanup_ok
                else:
                    fallback_cleanup_ok = False
            except BaseException:
                fallback_cleanup_ok = False
            for connection in (cleanup_parent, cleanup_child):
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass
            if cleanup_process is not None:
                try:
                    if cleanup_process.is_alive():
                        cleanup_process.terminate()
                        _join_process_with_deadline(cleanup_process, deadline)
                    if cleanup_process.is_alive():
                        cleanup_process.kill()
                        _join_process_with_deadline(cleanup_process, deadline, after_force_kill=True)
                except (OSError, RuntimeError, AssertionError):
                    fallback_cleanup_ok = False
            try:
                if process.is_alive():
                    process.kill()
                    _join_process_with_deadline(process, deadline, after_force_kill=True)
            except (OSError, RuntimeError, AssertionError):
                fallback_cleanup_ok = False
            _SUPERVISION_OWNERS.pop(supervision_token, None)
            if not fallback_cleanup_ok:
                return _safe_result("canary", "failed", error="canary_descendant_survived")
            return _safe_result("canary", "failed", error="canary_cleanup_setup_failed")
        assert cleanup_parent is not None and cleanup_child is not None and cleanup_process is not None
        cleanup_ok = False
        cleanup_result_received = False
        cleanup_worker_timed_out = False
        try:
            remaining = max(0.0, deadline - time.monotonic())
            if cleanup_parent.poll(remaining):
                try:
                    cleanup_payload = cleanup_parent.recv()
                    if isinstance(cleanup_payload, tuple) and len(cleanup_payload) == 2:
                        cleanup_ok = cleanup_payload[0] is True
                        if isinstance(cleanup_payload[1], dict):
                            tracked_children.update({int(pid): int(starttime) for pid, starttime in cleanup_payload[1].items()})
                    cleanup_result_received = True
                except (EOFError, OSError, ValueError, TypeError):
                    cleanup_ok = False
            else:
                cleanup_worker_timed_out = True
            if cleanup_process.is_alive():
                cleanup_process.terminate()
                _join_process_with_deadline(cleanup_process, deadline)
                if cleanup_process.is_alive():
                    cleanup_process.kill()
                    _join_process_with_deadline(cleanup_process, deadline, after_force_kill=True)
                    cleanup_ok = False
            _join_process_with_deadline(process, deadline)
            if process.pid is not None and root_starttime is not None and time.monotonic() < deadline:
                cleanup_ok = _track_adopted_descendants(os.getpid(), baseline_children, tracked_children) and cleanup_ok
                cleanup_ok = _token_owned_processes(supervision_token, tracked_children) and cleanup_ok
                cleanup_ok = _kill_tracked_processes(process.pid, baseline_children, tracked_children, root_starttime, deadline=deadline) and cleanup_ok
                cleanup_ok = _reap_adopted_children(tracked_children, deadline=deadline) and cleanup_ok
            else:
                cleanup_ok = False
            if process.is_alive():
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass
                _join_process_with_deadline(process, deadline, after_force_kill=True)
            cleanup_ok = cleanup_ok and not process.is_alive()
        finally:
            cleanup_parent.close()
        if cleanup_worker_timed_out or not cleanup_result_received or time.monotonic() >= deadline:
            result = _safe_result("canary", "failed", error="canary_cleanup_deadline_exceeded")
        elif not cleanup_ok:
            result = _safe_result("canary", "failed", error="canary_descendant_survived")
    _SUPERVISION_OWNERS.pop(supervision_token, None)
    return result


def dry_run() -> dict[str, Any]:
    return {
        "status": "dry_run",
        "live": False,
        "network_called": False,
        "providers": {
            "video": "higgsfield/seedance_2_0",
            "voiceover": "elevenlabs/text-to-speech",
            "music": "higgsfield/seed_audio",
        },
        "required_live_gates": [
            "--live",
            "--confirm-spend",
            "SOLO_STUDIO_PROVIDER_CANARY_LIVE=1",
            "SOLO_STUDIO_ENABLE_HIGGSFIELD=1",
            "SOLO_STUDIO_ENABLE_TTS=1",
            "SOLO_STUDIO_HIGGSFIELD_ALLOWED_HOSTS=<comma-separated exact hosts>",
            "ffprobe available on PATH",
        ],
    }


def live_run(*, confirm_spend: bool = False) -> tuple[int, dict[str, Any]]:
    if confirm_spend is not True:
        return 2, {"status": "blocked", "reason": "spend_confirmation_required"}
    if not _truthy(os.environ.get("SOLO_STUDIO_PROVIDER_CANARY_LIVE")):
        return 2, {"status": "blocked", "reason": "live_canary_gate_missing"}
    if not _truthy(os.environ.get("SOLO_STUDIO_ENABLE_HIGGSFIELD")):
        return 2, {"status": "blocked", "reason": "higgsfield_enable_gate_missing"}
    if not _truthy(os.environ.get("SOLO_STUDIO_ENABLE_TTS")):
        return 2, {"status": "blocked", "reason": "tts_enable_gate_missing"}
    if not shutil.which("higgsfield"):
        return 2, {"status": "blocked", "reason": "higgsfield_cli_missing"}
    if not shutil.which("ffprobe"):
        return 2, {"status": "blocked", "reason": "ffprobe_missing"}
    if not os.environ.get("SOLO_STUDIO_HIGGSFIELD_ALLOWED_HOSTS", "").strip():
        return 2, {"status": "blocked", "reason": "higgsfield_host_allowlist_missing"}

    checks: list[dict[str, Any]] = []
    deadline = time.monotonic() + CANARY_TOTAL_TIMEOUT_SECONDS
    with tempfile.TemporaryDirectory(prefix="hermes-verify-provider-canary-", dir="/tmp") as directory:
        root = Path(directory)
        for check_name in ("video", "voiceover", "music"):
            if time.monotonic() >= deadline:
                checks.append(_safe_result("canary", "failed", error="canary_deadline_exceeded"))
                return 1, {"status": "failed", "live": True, "checks": checks}
            raw_result = _run_check_bounded(check_name, root, deadline)
            result = _safe_result(
                str(raw_result.get("provider", "unknown")),
                str(raw_result.get("status", "failed")),
                raw_result,
                str(raw_result["error_type"]) if raw_result.get("error_type") else None,
            )
            if time.monotonic() >= deadline:
                result = _safe_result(result["provider"], "failed", error="canary_deadline_exceeded")
            checks.append(result)
            if result["status"] != "passed":
                return 1, {"status": "failed", "live": True, "checks": checks}
    return 0, {"status": "passed", "live": True, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the explicitly gated Solo Studio provider canary")
    parser.add_argument("--live", action="store_true", help="allow one real provider run")
    parser.add_argument("--confirm-spend", action="store_true", help="confirm that provider credits may be consumed")
    args = parser.parse_args(argv)
    if not args.live:
        if args.confirm_spend:
            parser.error("--confirm-spend requires --live")
        print(json.dumps(dry_run(), sort_keys=True))
        return 0
    if not args.confirm_spend:
        parser.error("--live requires --confirm-spend")
    status, report = live_run(confirm_spend=args.confirm_spend)
    print(json.dumps(report, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
