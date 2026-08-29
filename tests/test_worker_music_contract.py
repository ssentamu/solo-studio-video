import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import music_generation
import worker


class WorkerMusicContractTests(unittest.TestCase):
    def _root(self):
        return tempfile.TemporaryDirectory(dir="/opt/data", prefix="hermes-verify-")

    def _metadata(self, *, bytes_count=128, duration=30.0):
        return {
            "status": "downloaded",
            "provider": "higgsfield",
            "bytes": bytes_count,
            "duration_seconds": duration,
            "audio_verified": True,
        }

    def test_malformed_storyboard_duration_never_reaches_provider(self):
        for duration in (True, "30", 0, -1, float("inf")):
            with self.subTest(duration=duration), self._root() as directory:
                root = Path(directory)
                (root / "music_prompt.txt").write_text("ambient", encoding="utf-8")
                with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}, clear=False), patch(
                    "worker.generate_music"
                ) as generate:
                    self.assertFalse(worker._generate_music(root, {"total_duration": duration}))
                generate.assert_not_called()

    def test_provider_metadata_bounds_and_artifact_verification_are_required(self):
        with self._root() as directory:
            root = Path(directory)
            (root / "music_prompt.txt").write_text("ambient", encoding="utf-8")
            metadata = self._metadata(bytes_count=10**100, duration=10**100)
            with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}, clear=False), patch(
                "worker.generate_music", return_value=metadata
            ), patch("worker._probe_media", return_value={"valid": True, "size_bytes": 1, "duration_seconds": 1.0}):
                self.assertFalse(worker._generate_music(root, {"total_duration": 10}))
            self.assertFalse((root / "audio" / "music_metadata.json").exists())

    def test_provider_result_must_leave_a_matching_verified_audio_artifact(self):
        with self._root() as directory:
            root = Path(directory)
            (root / "music_prompt.txt").write_text("ambient", encoding="utf-8")
            metadata = self._metadata(bytes_count=128, duration=10.0)
            with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}, clear=False), patch(
                "worker.generate_music", return_value=metadata
            ), patch("worker._probe_media", return_value={"valid": False}):
                self.assertFalse(worker._generate_music(root, {"total_duration": 10}))
            self.assertFalse((root / "audio" / "music_metadata.json").exists())

    def test_worker_rejects_canonical_replacement_during_music_verification(self):
        with self._root() as directory:
            root = Path(directory)
            audio = root / "audio" / "background_music.mp3"
            (root / "music_prompt.txt").write_text("ambient", encoding="utf-8")
            metadata = self._metadata(bytes_count=128, duration=10.0)

            def generate_music(prompt, duration, destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"x" * 128)
                identity = os.stat(destination, follow_symlinks=False)
                metadata["artifact_identity"] = (identity.st_dev, identity.st_ino)
                metadata["artifact_sha256"] = "a" * 64
                return metadata

            def replace_after_open(path, **kwargs):
                replacement = Path(path).with_name("replacement.mp3")
                replacement.write_bytes(b"y" * 128)
                os.replace(replacement, path)
                return {
                    "valid": True,
                    "size_bytes": 128,
                    "duration_seconds": 10.0,
                    "sha256": "a" * 64,
                }

            with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}, clear=False), patch(
                "worker.generate_music", side_effect=generate_music
            ), patch("worker._probe_media", side_effect=replace_after_open):
                self.assertFalse(worker._generate_music(root, {"total_duration": 10}))
            self.assertFalse((root / "audio" / "music_metadata.json").exists())

    def test_music_adapter_normalizes_extreme_duration_overflow(self):
        with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}, clear=False):
            with self.assertRaises(music_generation.MusicGenerationError):
                music_generation.generate_music("ambient", 10**1000, Path("/tmp/unused.mp3"))

    def test_music_cleanup_forwards_absolute_deadline(self):
        with self._root() as directory:
            root = Path(directory)
            artifact = root / "temporary.mp3"
            artifact.write_bytes(b"data")
            directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            descriptor = os.open(artifact, os.O_RDONLY)
            deadline = 123.456
            try:
                with patch("music_generation._remove_entry_at") as remove:
                    music_generation._cleanup(directory_fd, artifact.name, descriptor, deadline=deadline)
                remove.assert_called_once()
                self.assertEqual(remove.call_args.kwargs["deadline"], deadline)
            finally:
                os.close(descriptor)
                os.close(directory_fd)

    def test_music_download_forwards_absolute_deadline_to_all_cleanup_calls(self):
        class Response:
            def __init__(self):
                self._chunks = [b"x" * 1024, b""]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return self._chunks.pop(0)

        with self._root() as directory:
            root = Path(directory)
            destination = root / "music.mp3"
            deadline = time.monotonic() + 30.0
            with patch("music_generation._open_provider_url", return_value=Response()), patch(
                "music_generation.probe_media",
                return_value={"has_audio": True, "has_video": False, "duration_seconds": 5.0, "sha256": "a" * 64},
            ), patch("music_generation._remove_entry_at") as remove:
                result = music_generation._download_verified_audio(
                    "https://provider.invalid/audio", destination, deadline=deadline
                )
            self.assertTrue(result["audio_verified"])
            self.assertGreaterEqual(remove.call_count, 2)
            for call in remove.call_args_list:
                self.assertEqual(call.kwargs["deadline"], deadline)


if __name__ == "__main__":
    unittest.main()
