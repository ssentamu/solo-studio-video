import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import media_assembly
import worker
from package_utils import compute_package_status


class MediaAssemblyTests(unittest.TestCase):
    def test_ffprobe_duplicate_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.mp4"
            path.write_bytes(b"not-empty")
            result = subprocess.CompletedProcess(
                ["ffprobe"],
                0,
                '{"streams": [], "streams": [], "format": {"duration": "1"}}',
                "",
            )
            with patch("media_assembly.shutil.which", return_value="ffprobe"), patch(
                "media_assembly._run_bounded_subprocess", return_value=result
            ):
                with self.assertRaises(media_assembly.MediaError):
                    media_assembly.probe_media(path)

    def test_ffprobe_supervision_error_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.mp4"
            path.write_bytes(b"not-empty")
            with patch("media_assembly.shutil.which", return_value="ffprobe"), patch(
                "media_assembly._run_bounded_subprocess",
                side_effect=subprocess.SubprocessError("supervision failed"),
            ):
                with self.assertRaises(media_assembly.MediaError) as caught:
                    media_assembly.probe_media(path)
            self.assertIn("supervision", str(caught.exception))

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

            metadata = {"duration_seconds": 4.0, "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}]}
            lease_checks = []
            with patch("media_assembly.shutil.which", side_effect=["ffprobe", "ffmpeg", "ffprobe"]), patch(
                "media_assembly.probe_media", return_value=metadata
            ), patch("media_assembly._run_bounded_subprocess", side_effect=fake_run):
                result = media_assembly.assemble_verified_clips(
                    clips,
                    output,
                    expected_duration=4.0,
                    lease_check=lambda: lease_checks.append(True),
                )

            self.assertTrue(output.exists())
            self.assertEqual(result["path"], str(output))
            self.assertEqual(len(result["sha256"]), 64)
            self.assertEqual(result["width"], 1920)
            self.assertEqual(result["height"], 1080)
            self.assertGreaterEqual(len(lease_checks), 6)

    def test_assembly_rejects_mutation_during_temporary_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "scene_01.mp4"
            clip.write_bytes(b"clip-input")
            output = root / "final" / "video.mp4"
            assembled = b"assembled-output"
            metadata = {"duration_seconds": 1.0, "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}]}
            real_remove = media_assembly._remove_entry_at

            def cleanup_then_mutate(parent_fd, name, expected_inode=None, **kwargs):
                result = real_remove(parent_fd, name, expected_inode, **kwargs)
                if str(name).startswith(".video-") and output.exists():
                    output.write_bytes(b"mutated-output")
                return result

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(assembled)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("media_assembly.shutil.which", side_effect=["ffprobe", "ffmpeg", "ffprobe"]), patch(
                "media_assembly.probe_media", return_value=metadata
            ), patch("media_assembly._run_bounded_subprocess", side_effect=fake_run), patch(
                "media_assembly._remove_entry_at", side_effect=cleanup_then_mutate
            ):
                with self.assertRaises(media_assembly.MediaError):
                    media_assembly.assemble_verified_clips([clip], output, expected_duration=1.0)
            self.assertFalse(output.exists())

    def test_verify_mp4_enforces_expected_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.mp4"
            path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"video")
            metadata = {
                "duration_seconds": 4.0,
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}],
            }

            with patch("media_assembly.shutil.which", return_value="ffprobe"), patch(
                "media_assembly.probe_media", return_value=metadata
            ):
                result = media_assembly.verify_mp4(path, expected_width=1920, expected_height=1080)
                self.assertEqual(result["width"], 1920)
                self.assertEqual(result["height"], 1080)
                with self.assertRaises(media_assembly.MediaError):
                    media_assembly.verify_mp4(path, expected_width=1080, expected_height=1920)

    def test_empty_clip_set_fails_closed(self):
        with self.assertRaises(media_assembly.MediaError):
            media_assembly.assemble_verified_clips([], "/tmp/never-publish.mp4")

    def test_verify_mp4_rejects_invalid_duration_contract_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"not-used")
            with patch("media_assembly.shutil.which", return_value="ffprobe"):
                for expected_duration, tolerance in (
                    (0.0, 2.0), (-1.0, 2.0), (1.0, float("nan")), (1.0, -0.1),
                ):
                    with self.subTest(expected_duration=expected_duration, tolerance=tolerance):
                        with self.assertRaises(media_assembly.MediaError):
                            media_assembly.verify_mp4(
                                path, expected_duration=expected_duration, tolerance=tolerance,
                            )

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

            def fake_probe(path, **_kwargs):
                return {"path": str(path), "exists": True, "size_bytes": 1, "ffprobe_checked": True, "valid": True, "duration_seconds": 1.0}

            with patch("package_utils._probe_media", side_effect=fake_probe):
                summary = compute_package_status(root, "completed")

            self.assertFalse(summary["has_clips"])
            self.assertNotEqual(summary["package_status"], "final_video_ready")
            self.assertTrue(summary["artifact_errors"])

    def test_package_status_rejects_final_video_with_wrong_profile_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clips").mkdir()
            (root / "final").mkdir()
            profile = {"output_profile": "landscape", "aspect_ratio": "16:9", "resolution": "1920x1080"}
            (root / "storyboard.json").write_text(json.dumps({**profile, "scenes": [{"scene_number": 1}]}))
            clip = root / "clips" / "scene_01.mp4"
            clip.write_bytes(b"verified clip")
            plan = root / "clips" / "generation_plan.json"
            plan.write_text(json.dumps({
                **profile,
                "status": "completed",
                "total_scenes": 1,
                "scenes": [{
                    "scene_number": 1,
                    "status": "verified",
                    "target_file": "clips/scene_01.mp4",
                    "sha256": hashlib.sha256(clip.read_bytes()).hexdigest(),
                }],
            }))
            final = root / "final" / "video.mp4"
            final.write_bytes(b"wrong-dimension final")

            def fake_probe(path, **_kwargs):
                return {
                    "path": str(path), "exists": True, "size_bytes": 1,
                    "ffprobe_checked": True, "valid": True,
                    "duration_seconds": 1.0,
                    "width": 1080 if path == final else 1920,
                    "height": 1920 if path == final else 1080,
                }

            with patch("package_utils._probe_media", side_effect=fake_probe):
                summary = compute_package_status(
                    root,
                    "completed",
                    expected_final_sha256=hashlib.sha256(final.read_bytes()).hexdigest(),
                    expected_plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                )

            self.assertFalse(summary["final_video_profile_matches"])
            self.assertFalse(summary["has_final_video"])
            self.assertNotEqual(summary["package_status"], "final_video_ready")

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

            def fake_probe(path, **_kwargs):
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

    def test_worker_assembly_skips_dry_run_without_final_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clips").mkdir()
            (root / "clips" / "generation_plan.json").write_text(json.dumps({
                "status": "dry_run",
                "total_scenes": 1,
                "scenes": [{
                    "scene_number": 1,
                    "status": "dry_run",
                    "target_file": "clips/scene_01.mp4",
                }],
            }))
            storyboard = {"scenes": [{"scene_number": 1}], "total_duration": 5}

            self.assertIsNone(worker._assemble_verified_output(root, storyboard))
            self.assertFalse((root / "final" / "video.mp4").exists())

    def test_worker_assembly_fails_closed_when_completed_plan_lacks_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clips").mkdir()
            (root / "clips" / "generation_plan.json").write_text(json.dumps({
                "status": "completed",
                "total_scenes": 1,
                "scenes": [{
                    "scene_number": 1,
                    "status": "downloaded",
                    "target_file": "clips/scene_01.mp4",
                    "sha256": "a" * 64,
                }],
            }))
            storyboard = {"scenes": [{"scene_number": 1}], "total_duration": 5}

            with self.assertRaises(media_assembly.MediaError):
                worker._assemble_verified_output(root, storyboard)
            self.assertFalse((root / "final" / "video.mp4").exists())

    def test_worker_assembly_rejects_mismatched_storyboard_and_plan_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clips").mkdir()
            (root / "clips" / "generation_plan.json").write_text(json.dumps({
                "status": "completed",
                "total_scenes": 1,
                "output_profile": "vertical",
                "aspect_ratio": "9:16",
                "resolution": "1080x1920",
                "scenes": [{
                    "scene_number": 1,
                    "status": "downloaded",
                    "target_file": "clips/scene_01.mp4",
                    "sha256": "a" * 64,
                }],
            }))
            storyboard = {
                "output_profile": "landscape",
                "aspect_ratio": "16:9",
                "resolution": "1920x1080",
                "scenes": [{"scene_number": 1}],
                "total_duration": 5,
            }

            with self.assertRaises(media_assembly.MediaError):
                worker._assemble_verified_output(root, storyboard)

    def test_assemble_verified_clips_rejects_wrong_output_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "scene_01.mp4"
            clip.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"clip")
            output = root / "final" / "video.mp4"

            def fake_run(command, **kwargs):
                Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisom" + b"assembled")
                return subprocess.CompletedProcess(command, 0, "", "")

            metadata = {"duration_seconds": 4.0, "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920}]}
            with patch("media_assembly.shutil.which", side_effect=["ffprobe", "ffprobe", "ffmpeg", "ffprobe"]), patch(
                "media_assembly.probe_media", return_value=metadata
            ), patch("media_assembly._run_bounded_subprocess", side_effect=fake_run):
                with self.assertRaises(media_assembly.MediaError):
                    media_assembly.assemble_verified_clips(
                        [clip],
                        output,
                        expected_width=1920,
                        expected_height=1080,
                    )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
