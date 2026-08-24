import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import audio_generation


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        return b"ID3" + b"audio"


class AudioGenerationTests(unittest.TestCase):
    def test_disabled_generation_fails_without_network(self):
        with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_TTS": "0"}, clear=False):
            with self.assertRaises(audio_generation.AudioGenerationError):
                audio_generation.generate_voiceover("hello", Path(tempfile.gettempdir()) / "never.mp3")

    def test_transient_provider_failure_retries_and_publishes_verified_audio(self):
        error = urllib.error.HTTPError("https://provider.invalid", 503, "busy", cast(Any, None), None)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SOLO_STUDIO_ENABLE_TTS": "1", "ELEVENLABS_API_KEY": "test-secret", "SOLO_STUDIO_TTS_ATTEMPTS": "2"},
            clear=False,
        ), patch("audio_generation._open_tts_request", side_effect=[error, _Response()]), patch(
            "audio_generation.probe_media", return_value={"has_audio": True, "duration_seconds": 2.5}
        ), patch("audio_generation.time.sleep"):
            result = audio_generation.generate_voiceover("hello", Path(directory) / "voiceover.mp3")
            self.assertEqual(result["duration_seconds"], 2.5)
            self.assertTrue((Path(directory) / "voiceover.mp3").exists())


if __name__ == "__main__":
    unittest.main()
