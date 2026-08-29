import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import provider_canary
import music_generation
import engines.generation_agent as generation_agent
from music_generation import MusicGenerationError, generate_music
from package_utils import _contain_entry_at


class ProviderCanaryTests(unittest.TestCase):
    def test_cleanup_pipe_failure_is_normalized_and_provider_is_reaped(self):
        context = provider_canary.multiprocessing.get_context("fork")
        original_pipe = context.Pipe
        calls = 0

        def fail_second_pipe(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("cleanup pipe unavailable")
            return original_pipe(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            with patch("provider_canary.multiprocessing.get_context", return_value=context), patch(
                "provider_canary._enable_child_subreaper", return_value=True
            ), patch("provider_canary._direct_child_identities_state", return_value=({}, True)), patch.object(
                context, "Pipe", side_effect=fail_second_pipe
            ):
                result = provider_canary._run_check_bounded("video", Path(tmp), time.monotonic() + 1.0)
        self.assertEqual(result.get("error_type"), "canary_cleanup_setup_failed")
        self.assertEqual(provider_canary._SUPERVISION_OWNERS, {})
        self.assertEqual(provider_canary.multiprocessing.active_children(), [])

    def test_cleanup_setup_failure_kills_detached_descendants(self):
        context = provider_canary.multiprocessing.get_context("fork")
        original_pipe = context.Pipe
        calls = 0

        def fail_second_pipe(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("cleanup pipe unavailable")
            return original_pipe(*args, **kwargs)

        def child(_check_name, root, _deadline, connection):
            marker = Path(root) / "escaped-pid"
            pid = os.fork()
            if pid == 0:
                os.setsid()
                marker.write_text(str(os.getpid()), encoding="ascii")
                time.sleep(10)
                os._exit(0)
            connection.send({"provider": "probe", "status": "passed"})
            time.sleep(10)

        with tempfile.TemporaryDirectory(prefix="hermes-verify-", dir="/tmp") as directory:
            escaped_pid = None
            try:
                with patch("provider_canary._check_child", side_effect=child), patch(
                    "provider_canary.multiprocessing.get_context", return_value=context
                ), patch("provider_canary._enable_child_subreaper", return_value=True), patch(
                    "provider_canary._direct_child_identities_state", return_value=({}, True)
                ), patch.object(context, "Pipe", side_effect=fail_second_pipe):
                    result = provider_canary._run_check_bounded("video", Path(directory), time.monotonic() + 3.0)
                escaped_pid = int((Path(directory) / "escaped-pid").read_text(encoding="ascii"))
                identity = provider_canary._proc_identity(escaped_pid)
                self.assertEqual(result["error_type"], "canary_cleanup_setup_failed")
                self.assertIn(identity[2] if identity else "gone", {"gone", "Z"})
            finally:
                if escaped_pid is not None:
                    identity = provider_canary._proc_identity(escaped_pid)
                    if identity is not None and identity[2] != "Z":
                        try:
                            os.kill(escaped_pid, 9)
                        except ProcessLookupError:
                            pass

    def test_default_cli_is_no_network_dry_run(self):
        with patch("provider_canary.run_higgsfield") as video, patch("provider_canary.generate_voiceover") as voice, patch(
            "provider_canary.generate_music"
        ) as music:
            self.assertEqual(provider_canary.main([]), 0)
            video.assert_not_called()
            voice.assert_not_called()
            music.assert_not_called()

    def test_live_run_requires_explicit_gate(self):
        with patch.dict(os.environ, {}, clear=True):
            status, report = provider_canary.live_run(confirm_spend=True)
        self.assertEqual(status, 2)
        self.assertEqual(report["reason"], "live_canary_gate_missing")

    def test_bounded_check_terminates_child_after_ipc_result(self):
        def child(_check_name, _root, _deadline, connection):
            connection.send({"status": "passed"})
            time.sleep(10)

        with tempfile.TemporaryDirectory(prefix="hermes-verify-", dir="/tmp") as directory, patch(
            "provider_canary._check_child", side_effect=child
        ):
            started = time.monotonic()
            result = provider_canary._run_check_bounded("video", Path(directory), time.monotonic() + 2)
            elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "passed")
        self.assertLess(elapsed, 2)

    def test_bounded_check_fails_when_cleanup_exceeds_deadline(self):
        def child(_check_name, _root, _deadline, connection):
            connection.send({"status": "passed"})

        def slow_cleanup(*_args, **_kwargs):
            time.sleep(0.2)
            return True

        with tempfile.TemporaryDirectory(prefix="hermes-verify-", dir="/tmp") as directory, patch(
            "provider_canary._check_child", side_effect=child
        ), patch("provider_canary._kill_tracked_processes", side_effect=slow_cleanup):
            started = time.monotonic()
            result = provider_canary._run_check_bounded("video", Path(directory), started + 0.05)
            elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "canary_cleanup_deadline_exceeded")
        self.assertLess(elapsed, 0.15)

    def test_bounded_check_kills_detached_descendant(self):
        def child(_check_name, root, _deadline, connection):
            marker = Path(root) / "escaped-pid"
            pid = os.fork()
            if pid == 0:
                os.setsid()
                marker.write_text(str(os.getpid()), encoding="ascii")
                time.sleep(10)
                os._exit(0)
            time.sleep(0.1)
            connection.send({"provider": "probe", "status": "passed"})
            time.sleep(10)

        with tempfile.TemporaryDirectory(prefix="hermes-verify-", dir="/tmp") as directory, patch(
            "provider_canary._check_child", side_effect=child
        ):
            result = provider_canary._run_check_bounded("video", Path(directory), time.monotonic() + 3)
            escaped_pid = int((Path(directory) / "escaped-pid").read_text(encoding="ascii"))
            identity = provider_canary._proc_identity(escaped_pid)
        self.assertEqual(result["status"], "passed")
        self.assertIn(identity[2] if identity else "gone", {"gone", "Z"})

    def test_live_run_blocks_before_provider_call_without_ffprobe(self):
        env = {
            "SOLO_STUDIO_PROVIDER_CANARY_LIVE": "1",
            "SOLO_STUDIO_ENABLE_HIGGSFIELD": "1",
            "SOLO_STUDIO_ENABLE_TTS": "1",
        }

        def which(name):
            return "/usr/bin/higgsfield" if name == "higgsfield" else None

        with patch.dict(os.environ, env, clear=True), patch("provider_canary.shutil.which", side_effect=which), patch(
            "provider_canary._run_video"
        ) as video:
            status, report = provider_canary.live_run(confirm_spend=True)
        self.assertEqual(status, 2)
        self.assertEqual(report["reason"], "ffprobe_missing")
        video.assert_not_called()

    def test_live_run_requires_higgsfield_host_allowlist(self):
        env = {
            "SOLO_STUDIO_PROVIDER_CANARY_LIVE": "1",
            "SOLO_STUDIO_ENABLE_HIGGSFIELD": "1",
            "SOLO_STUDIO_ENABLE_TTS": "1",
        }
        with patch.dict(os.environ, env, clear=True), patch("provider_canary.shutil.which", return_value="/usr/bin/tool"):
            status, report = provider_canary.live_run(confirm_spend=True)
        self.assertEqual(status, 2)
        self.assertEqual(report["reason"], "higgsfield_host_allowlist_missing")

    def test_live_report_only_contains_safe_metadata(self):
        env = {
            "SOLO_STUDIO_PROVIDER_CANARY_LIVE": "1",
            "SOLO_STUDIO_ENABLE_HIGGSFIELD": "1",
            "SOLO_STUDIO_ENABLE_TTS": "1",
            "SOLO_STUDIO_HIGGSFIELD_ALLOWED_HOSTS": "provider.invalid",
        }
        with patch.dict(os.environ, env, clear=True), patch("provider_canary.shutil.which", return_value="/usr/bin/higgsfield"), patch(
            "provider_canary._run_video",
            return_value={"provider": "higgsfield-video", "status": "passed", "output_file": "/secret/path"},
        ), patch(
            "provider_canary._run_voiceover",
            return_value={"provider": "elevenlabs-tts", "status": "passed", "bytes": 10},
        ), patch(
            "provider_canary._run_music",
            return_value={"provider": "higgsfield-seed-audio", "status": "passed", "duration_seconds": 5.0},
        ):
            status, report = provider_canary.live_run(confirm_spend=True)
        self.assertEqual(status, 0)
        self.assertEqual(report["status"], "passed")
        self.assertNotIn("output_file", json.dumps(report))

    def test_music_provider_uses_seed_audio_and_sanitizes_failure(self):
        completed = SimpleNamespace(returncode=0, stdout=json.dumps({"status": "completed", "audio_url": "https://provider.invalid/audio"}), stderr="")
        with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1", "SOLO_STUDIO_PROVIDER_RETRY_ATTEMPTS": "1"}, clear=False), patch(
            "music_generation._run_bounded_subprocess", return_value=completed
        ) as run, patch(
            "music_generation._download_verified_audio",
            side_effect=MusicGenerationError("download failed")
        ):
            with self.assertRaises(MusicGenerationError):
                generate_music("short instrumental bed", 5, Path("/tmp/canary.mp3"))
        self.assertEqual(run.call_args.args[0][:4], ["higgsfield", "generate", "create", "seed_audio"])
        self.assertNotIn("--duration", run.call_args.args[0])

    def test_provider_url_requires_exact_allowlist(self):
        with patch.dict(os.environ, {"SOLO_STUDIO_HIGGSFIELD_ALLOWED_HOSTS": "cdn.provider.test"}, clear=False), patch(
            "engines.generation_agent._public_provider_ip", return_value="203.0.113.10"
        ):
            with self.assertRaises(ValueError):
                generation_agent._assert_safe_provider_url("https://other.provider.test/video.mp4")
            generation_agent._assert_safe_provider_url("https://cdn.provider.test/video.mp4")

    def test_containment_quarantines_replaced_canonical_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.mp3"
            replacement = root / "replacement.mp3"
            canonical = root / "audio.mp3"
            original.write_bytes(b"trusted")
            canonical.hardlink_to(original)
            expected = (original.stat().st_dev, original.stat().st_ino)
            canonical.unlink()
            replacement.write_bytes(b"untrusted")
            replacement.rename(canonical)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                _contain_entry_at(directory_fd, "audio.mp3", expected, "test-audio")
            finally:
                os.close(directory_fd)
            self.assertFalse(canonical.exists())
            self.assertTrue(any(root.glob(".test-audio.untrusted-*")))

    def test_containment_uses_atomic_exchange_when_pathname_rename_is_blocked(self):
        import package_utils

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.mp3"
            canonical = root / "audio.mp3"
            original.write_bytes(b"trusted")
            canonical.hardlink_to(original)
            expected = (original.stat().st_dev, original.stat().st_ino)
            canonical.unlink()
            canonical.write_bytes(b"untrusted")
            real_rename = package_utils.os.rename

            def block_canonical_rename(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
                if destination.startswith(".test-exchange"):
                    raise OSError("injected pathname quarantine failure")
                return real_rename(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch("package_utils.os.rename", side_effect=block_canonical_rename):
                    _contain_entry_at(directory_fd, "audio.mp3", expected, "test-exchange")
            finally:
                os.close(directory_fd)
            self.assertFalse(canonical.exists())
            self.assertTrue(any(root.glob(".test-exchange.untrusted-*")))

    def test_containment_fails_closed_when_both_quarantine_paths_fail(self):
        import package_utils

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.mp3"
            canonical = root / "audio.mp3"
            original.write_bytes(b"trusted")
            canonical.hardlink_to(original)
            expected = (original.stat().st_dev, original.stat().st_ino)
            canonical.unlink()
            canonical.write_bytes(b"untrusted")
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch("package_utils._rename_exchange", side_effect=OSError("exchange blocked")), patch(
                    "package_utils.os.rename", side_effect=OSError("rename blocked")
                ):
                    with self.assertRaises(OSError):
                        _contain_entry_at(directory_fd, "audio.mp3", expected, "test-both-fail")
            finally:
                os.close(directory_fd)
            self.assertTrue(canonical.exists())
            self.assertEqual(canonical.read_bytes(), b"untrusted")
            self.assertFalse(any(root.glob(".test-both-fail.untrusted-*")))

    def test_music_provider_rejects_failed_envelope_without_download(self):
        failed = SimpleNamespace(returncode=0, stdout=json.dumps({"status": "failed", "audio_url": "https://provider.invalid/audio"}), stderr="")
        with patch("music_generation._run_bounded_subprocess", return_value=failed), patch(
            "music_generation._download_verified_audio"
        ) as download:
            with self.assertRaises(MusicGenerationError):
                generate_music("short instrumental bed", 5, Path("/tmp/canary.mp3"))
        download.assert_not_called()

    def test_music_download_handles_partial_os_writes(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                if getattr(self, "done", False):
                    return b""
                self.done = True
                return b"synthetic-audio"

        original_write = os.write
        with tempfile.TemporaryDirectory() as directory, patch(
            "music_generation._open_provider_url", return_value=Response()
        ), patch(
            "music_generation.probe_media",
            return_value={"has_audio": True, "duration_seconds": 5.0, "sha256": "a" * 64},
        ), patch("music_generation.os.write", wraps=os.write) as write:
            def partial_write(fd, data):
                return original_write(fd, data[:1])

            write.side_effect = partial_write
            result = music_generation._download_verified_audio(
                "https://provider.invalid/audio", Path(directory) / "music.mp3"
            )
        self.assertTrue(result["audio_verified"])
        self.assertGreater(write.call_count, 1)


if __name__ == "__main__":
    unittest.main()
