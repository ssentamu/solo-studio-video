import os
import multiprocessing
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import audio_generation
import music_generation


class _Response:
    def __init__(self):
        self._done = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        if self._done:
            return b""
        self._done = True
        return b"ID3" + b"a" * 1021


class _ReadTimeoutResponse(_Response):
    def read(self, _size):
        raise socket.timeout("provider read timed out")


class AudioGenerationTests(unittest.TestCase):
    def test_expired_literal_ip_deadline_is_rejected(self):
        with self.assertRaises(audio_generation.AudioGenerationError):
            audio_generation._public_addresses("93.184.216.34", 443, deadline=time.monotonic() - 1)

    def test_dns_pipe_failure_releases_slot(self):
        with patch("audio_generation.multiprocessing.Pipe", side_effect=OSError("pipe unavailable")):
            with self.assertRaises(audio_generation.AudioGenerationError):
                audio_generation._public_addresses("example.test", 443, deadline=time.monotonic() + 0.1)
        self.assertTrue(audio_generation._TTS_DNS_SLOT.acquire(blocking=False))
        audio_generation._TTS_DNS_SLOT.release()

    def test_dns_child_eof_is_normalized_and_releases_slot(self):
        def eof_worker(_hostname, _port, pipe):
            pipe.close()

        with patch("audio_generation._dns_resolve_worker", eof_worker):
            with self.assertRaises(audio_generation.AudioGenerationError):
                audio_generation._public_addresses("example.test", 443, deadline=time.monotonic() + 0.5)
        self.assertTrue(audio_generation._TTS_DNS_SLOT.acquire(blocking=False))
        audio_generation._TTS_DNS_SLOT.release()

    def test_dns_reap_does_not_add_fixed_wait_after_deadline(self):
        class StubbornProcess:
            def __init__(self):
                self.calls = []

            def is_alive(self):
                return True

            def terminate(self):
                self.calls.append("terminate")

            def kill(self):
                self.calls.append("kill")

            def join(self, value):
                self.calls.append(value)

        process = StubbornProcess()
        started = time.monotonic()
        self.assertFalse(audio_generation._reap_dns_worker(process, deadline=started, force=True))
        self.assertEqual(process.calls, ["terminate", 0.0, "kill", 0.01])
        self.assertLess(time.monotonic() - started, 0.05)

    def test_dns_deadline_reaps_worker_and_releases_slot(self):
        def slow_resolver(*_args, **_kwargs):
            time.sleep(0.2)
            return []

        with patch("audio_generation.socket.getaddrinfo", side_effect=slow_resolver):
            with self.assertRaises(audio_generation.AudioGenerationError):
                audio_generation._public_addresses("example.test", 443, deadline=time.monotonic() + 0.03)
        self.assertTrue(audio_generation._TTS_DNS_SLOT.acquire(blocking=False))
        audio_generation._TTS_DNS_SLOT.release()
        self.assertFalse(any(process.name == "tts-dns" and process.is_alive() for process in multiprocessing.active_children()))
        self.assertFalse(any(thread.name == "tts-dns" and thread.is_alive() for thread in threading.enumerate()))

    def test_music_temporary_cleanup_oserror_is_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "temporary.mp3"
            artifact.write_bytes(b"data")
            directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                with patch("music_generation.os.stat", side_effect=OSError("cleanup failed")):
                    with self.assertRaises(music_generation.MusicGenerationError) as caught:
                        music_generation._cleanup(directory_fd, artifact.name, -1)
                self.assertIn("cleanup", str(caught.exception))
            finally:
                os.close(directory_fd)

    def test_music_supervision_error_is_normalized(self):
        with patch.dict(
            os.environ,
            {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1", "SOLO_STUDIO_HIGGSFIELD_TIMEOUT": "1"},
            clear=False,
        ), patch(
            "music_generation._run_bounded_subprocess",
            side_effect=subprocess.SubprocessError("supervision failed"),
        ):
            with self.assertRaises(music_generation.MusicGenerationError) as caught:
                music_generation.generate_music("music", 1.0, Path(tempfile.gettempdir()) / "unused.mp3")
        self.assertIn("supervision", str(caught.exception))

    def test_disabled_generation_fails_without_network(self):
        with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_TTS": "0"}, clear=False):
            with self.assertRaises(audio_generation.AudioGenerationError):
                audio_generation.generate_voiceover("hello", Path(tempfile.gettempdir()) / "never.mp3")

    def test_oversized_text_is_rejected_before_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SOLO_STUDIO_ENABLE_TTS": "1", "ELEVENLABS_API_KEY": "test-secret"},
            clear=False,
        ), patch("audio_generation._validate_public_tts_destination"), patch("audio_generation._open_tts_request") as request:
            with self.assertRaises(audio_generation.AudioGenerationError):
                audio_generation.generate_voiceover("x" * 20001, Path(directory) / "voiceover.mp3")
            request.assert_not_called()

    def test_transient_provider_failure_does_not_retry_billable_submission(self):
        error = urllib.error.HTTPError("https://provider.invalid", 503, "busy", cast(Any, None), None)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SOLO_STUDIO_ENABLE_TTS": "1", "ELEVENLABS_API_KEY": "test-secret", "SOLO_STUDIO_TTS_ATTEMPTS": "2"},
            clear=False,
        ), patch("audio_generation._open_tts_request", side_effect=error) as request, patch(
            "audio_generation.time.sleep"
        ):
            with self.assertRaises(audio_generation.AudioGenerationError):
                audio_generation.generate_voiceover("hello", Path(directory) / "voiceover.mp3")
            request.assert_called_once()

    def test_post_response_local_failure_does_not_retry_provider_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SOLO_STUDIO_ENABLE_TTS": "1", "ELEVENLABS_API_KEY": "test-secret", "SOLO_STUDIO_TTS_ATTEMPTS": "3"},
            clear=False,
        ), patch("audio_generation._open_tts_request", return_value=_Response()) as request, patch(
            "audio_generation.probe_media", return_value={"has_audio": True, "duration_seconds": 2.5}
        ), patch("audio_generation.os.fsync", side_effect=OSError("disk full")), patch(
            "audio_generation.time.sleep"
        ):
            with self.assertRaises(audio_generation.AudioGenerationError):
                audio_generation.generate_voiceover("hello", Path(directory) / "voiceover.mp3")
            request.assert_called_once()

    def test_tts_durability_runs_inside_publication_lock(self):
        class LockTracker:
            def __init__(self):
                self.held = False
                self.fsync_held = False

            def __enter__(self):
                self.held = True
                return self

            def __exit__(self, *_args):
                self.held = False
                return False

        tracker = LockTracker()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SOLO_STUDIO_ENABLE_TTS": "1", "ELEVENLABS_API_KEY": "test-secret"},
            clear=False,
        ), patch("audio_generation._open_tts_request", return_value=_Response()), patch(
            "audio_generation.probe_media", return_value={"has_audio": True, "duration_seconds": 2.5}
        ), patch("audio_generation._publication_lock", return_value=tracker), patch(
            "audio_generation._fsync_verified_publication", side_effect=lambda *args, **kwargs: setattr(tracker, "fsync_held", tracker.held)
        ):
            result = audio_generation.generate_voiceover("hello", Path(directory) / "voiceover.mp3")
        self.assertEqual(result["duration_seconds"], 2.5)
        self.assertTrue(tracker.fsync_held)

    def test_response_read_timeout_does_not_retry_provider_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SOLO_STUDIO_ENABLE_TTS": "1", "ELEVENLABS_API_KEY": "test-secret", "SOLO_STUDIO_TTS_ATTEMPTS": "3"},
            clear=False,
        ), patch("audio_generation._open_tts_request", return_value=_ReadTimeoutResponse()) as request, patch(
            "audio_generation.time.sleep"
        ):
            with self.assertRaises(audio_generation.AudioGenerationError):
                audio_generation.generate_voiceover("hello", Path(directory) / "voiceover.mp3")
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
