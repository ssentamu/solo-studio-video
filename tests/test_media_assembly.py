import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import media_assembly
from package_utils import compute_package_status


class MediaAssemblyTests(unittest.TestCase):
    def test_package_status_rejects_symlinked_final_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "job"
            external = Path(tmp) / "external.mp4"
            (root / "final").mkdir(parents=True)
            external.write_bytes(b"not-a-real-video")
            (root / "final" / "video.mp4").symlink_to(external)
            summary = compute_package_status(root)
            self.assertFalse(summary["has_final_video"])
            self.assertNotEqual(summary["package_status"], "final_video_ready")

    def test_assembly_is_atomic_and_returns_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = []
            for number in (1, 2):
                path = root / f"scene_{number:02d}.mp4"
                path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"clip")
                clips.append(path)
            output = root / "final" / "video.mp4"

            def fake_run(command, **kwargs):
                Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisom" + b"assembled")
                return subprocess.CompletedProcess(command, 0, "", "")

            metadata = {"duration_seconds": 4.0, "streams": [{"codec_type": "video", "codec_name": "h264"}]}
            lease_checks = []
            with patch("media_assembly.shutil.which", side_effect=["ffprobe", "ffmpeg", "ffprobe"]), patch(
                "media_assembly.probe_media", return_value=metadata
            ), patch("media_assembly.subprocess.run", side_effect=fake_run):
                result = media_assembly.assemble_verified_clips(
                    clips,
                    output,
                    expected_duration=4.0,
                    lease_check=lambda: lease_checks.append(True),
                )

            self.assertTrue(output.exists())
            self.assertEqual(result["path"], str(output))
            self.assertEqual(len(result["sha256"]), 64)
            self.assertGreaterEqual(len(lease_checks), 6)

    def test_empty_clip_set_fails_closed(self):
        with self.assertRaises(media_assembly.MediaError):
            media_assembly.assemble_verified_clips([], "/tmp/never-publish.mp4")

    def test_final_video_requires_matching_generation_plan_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clips").mkdir()
            (root / "final").mkdir()
            (root / "storyboard.json").write_text(json.dumps({"scenes": [{"scene_number": 1}, {"scene_number": 2}]}))
            (root / "clips" / "generation_plan.json").write_text(
                json.dumps({"status": "completed", "total_scenes": 2, "scenes": [{"scene_number": 1}, {"scene_number": 3}]})
            )
            for path in (root / "clips" / "scene_01.mp4", root / "clips" / "scene_02.mp4", root / "final" / "video.mp4"):
                path.write_bytes(b"verified")

            def fake_probe(path):
                return {"path": str(path), "exists": True, "size_bytes": 1, "ffprobe_checked": True, "valid": True, "duration_seconds": 1.0}

            with patch("package_utils._probe_media", side_effect=fake_probe):
                summary = compute_package_status(root, "completed")

            self.assertFalse(summary["has_clips"])
            self.assertNotEqual(summary["package_status"], "final_video_ready")
            self.assertTrue(summary["artifact_errors"])

    def test_package_status_rejects_clip_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clips").mkdir()
            (root / "final").mkdir()
            (root / "storyboard.json").write_text(json.dumps({"scenes": [{"scene_number": 1}]}))
            clip = root / "clips" / "scene_01.mp4"
            clip.write_bytes(b"actual clip bytes")
            (root / "clips" / "generation_plan.json").write_text(json.dumps({
                "status": "completed",
                "total_scenes": 1,
                "scenes": [{
                    "scene_number": 1,
                    "status": "verified",
                    "sha256": hashlib.sha256(b"different clip bytes").hexdigest(),
                }],
            }))
            (root / "final" / "video.mp4").write_bytes(b"verified")

            def fake_probe(path):
                return {"path": str(path), "exists": True, "size_bytes": 1,
                        "ffprobe_checked": True, "valid": True,
                        "duration_seconds": 1.0}

            with patch("package_utils._probe_media", side_effect=fake_probe):
                summary = compute_package_status(root, "completed")

            self.assertFalse(summary["has_clips"])
            self.assertFalse(summary["has_final_video"])

    def test_package_status_rejects_noncanonical_clip_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "storyboard.json").write_text(json.dumps({"scenes": [{"scene_number": 1}]}))
            (root / "clips").mkdir()
            (root / "clips" / "generation_plan.json").write_text(
                json.dumps({
                    "status": "completed",
                    "total_scenes": 1,
                    "scenes": [{"scene_number": 1, "status": "verified"}],
                })
            )
            (root / "clips" / "scene_001.mp4").write_bytes(b"not canonical")
            summary = compute_package_status(root)
            self.assertNotEqual(summary["package_status"], "final_video_ready")
            self.assertEqual(summary["verified_clips"], 0)
            self.assertIn("scene_001.mp4", summary["invalid_clip_files"])


if __name__ == "__main__":
    unittest.main()
