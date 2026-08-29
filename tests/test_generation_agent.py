import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engines.generation_agent import (
    _publish_staged_clips_descriptor,
    _public_provider_ip,
    _verify_clip,
    generate_plan,
    run_fake_provider,
    run_higgsfield,
)
from engines.script_agent import VideoFormat, generate_chapter_plan, generate_scenes
from media_assembly import MediaError, assemble_verified_clips
from package_utils import compute_package_status, write_package_manifest


class GenerationAgentTests(unittest.TestCase):
    def test_long_and_documentary_storyboards_preserve_requested_duration(self):
        for fmt, duration in ((VideoFormat.LONG, 601.0), (VideoFormat.DOCUMENTARY, 1800.0)):
            with self.subTest(fmt=fmt):
                chapters = generate_chapter_plan(duration, fmt, ["message one", "message two", "message three"])
                scenes = generate_scenes(chapters, "topic", "professional", "youtube")
                self.assertAlmostEqual(sum(chapter.duration for chapter in chapters), duration, places=6)
                self.assertAlmostEqual(sum(scene.duration_seconds for scene in scenes), duration, places=6)

    def test_provider_clip_verification_rejects_requested_duration_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")
            with patch("engines.generation_agent.shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "engines.generation_agent._run_bounded_subprocess",
                return_value=type(
                    "Completed", (), {
                        "returncode": 0,
                        "stdout": json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "1.0"}}),
                        "stderr": "",
                    }
                )(),
            ):
                valid, reason = _verify_clip(clip, expected_duration=5.0)
            self.assertFalse(valid)
            self.assertIn("requested scene duration", reason or "")

    def test_provider_clip_supervision_error_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")
            with patch("engines.generation_agent.shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "engines.generation_agent._run_bounded_subprocess",
                side_effect=subprocess.SubprocessError("supervision failed"),
            ):
                valid, reason = _verify_clip(clip)
            self.assertFalse(valid)
            self.assertIn("could not verify", reason or "")

    def test_fake_provider_cleans_published_clip_when_fsync_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "clips" / "scene_01.mp4"
            clip.parent.mkdir()
            profile = {
                "output_profile": "landscape",
                "aspect_ratio": "16:9",
                "resolution": "1920x1080",
                "width": 1920,
                "height": 1080,
            }
            with patch("engines.generation_agent.os.fsync", side_effect=OSError("injected fsync failure")):
                result = run_fake_provider("cleanup", 0.2, clip, profile)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(clip.exists())

    def test_fake_provider_cleanup_failure_raises_instead_of_returning_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "clips" / "scene_01.mp4"
            clip.parent.mkdir()
            profile = {
                "output_profile": "landscape",
                "aspect_ratio": "16:9",
                "resolution": "1920x1080",
                "width": 1920,
                "height": 1080,
            }
            with patch("engines.generation_agent._cleanup_temporary", side_effect=OSError("injected cleanup failure")):
                with self.assertRaises(RuntimeError):
                    run_fake_provider("cleanup", 0.2, clip, profile)

    def test_higgsfield_malformed_timeout_is_controlled_failure(self):
        with patch.dict(os.environ, {"SOLO_STUDIO_HIGGSFIELD_TIMEOUT": "not-an-integer", "SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
            "engines.generation_agent._run_bounded_subprocess",
            side_effect=OSError("higgsfield unavailable"),
        ):
            from engines.generation_agent import run_higgsfield

            result = run_higgsfield("timeout", 1.0, Path("/tmp/unused.mp4"), "model")
        self.assertEqual(result["status"], "failed")
        self.assertIn("could not start", result["error"])

    def test_higgsfield_supervision_error_is_controlled_failure(self):
        with patch.dict(os.environ, {"SOLO_STUDIO_HIGGSFIELD_TIMEOUT": "1", "SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
            "engines.generation_agent._run_bounded_subprocess",
            side_effect=subprocess.SubprocessError("supervision failed"),
        ):
            result = run_higgsfield("supervision", 1.0, Path("/tmp/unused.mp4"), "model")
        self.assertEqual(result["status"], "failed")
        self.assertIn("supervision", result["error"])

    def test_assembly_rejects_mixed_profile_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips_dir = root / "clips"
            clips_dir.mkdir()
            landscape = {
                "output_profile": "landscape",
                "aspect_ratio": "16:9",
                "resolution": "1920x1080",
                "width": 1920,
                "height": 1080,
            }
            vertical = {
                "output_profile": "vertical",
                "aspect_ratio": "9:16",
                "resolution": "1080x1920",
                "width": 1080,
                "height": 1920,
            }
            first = clips_dir / "scene_01.mp4"
            second = clips_dir / "scene_02.mp4"
            self.assertEqual(run_fake_provider("landscape", 0.4, first, landscape)["status"], "downloaded")
            self.assertEqual(run_fake_provider("vertical", 0.4, second, vertical)["status"], "downloaded")

            with self.assertRaises(MediaError):
                assemble_verified_clips(
                    [first, second],
                    root / "final" / "video.mp4",
                    expected_width=1920,
                    expected_height=1080,
                )
            self.assertFalse((root / "final" / "video.mp4").exists())

    def test_assembly_rejects_clip_duration_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = {
                "output_profile": "landscape",
                "aspect_ratio": "16:9",
                "resolution": "1920x1080",
                "width": 1920,
                "height": 1080,
            }
            clip = root / "scene_01.mp4"
            self.assertEqual(run_fake_provider("duration", 0.4, clip, profile)["status"], "downloaded")

            with self.assertRaises(MediaError):
                assemble_verified_clips(
                    [clip],
                    root / "final" / "video.mp4",
                    expected_duration=0.4,
                    expected_width=1920,
                    expected_height=1080,
                    expected_clip_durations={"scene_01.mp4": 2.0},
                )

    def test_fake_provider_generates_verified_vertical_video_to_final_ready(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"SOLO_STUDIO_VIDEO_PROVIDER": "fake"},
            clear=False,
        ):
            root = Path(tmp)
            profile = {
                "output_profile": "vertical",
                "aspect_ratio": "9:16",
                "resolution": "1080x1920",
            }
            scenes = [
                {"scene_number": 1, "duration_seconds": 0.4, "visual_description": "opening"},
                {"scene_number": 2, "duration_seconds": 0.4, "visual_description": "ending"},
            ]
            storyboard = {**profile, "total_duration": 0.8, "scenes": scenes}
            (root / "storyboard.json").write_text(json.dumps(storyboard), encoding="utf-8")
            prompts_path = root / "video_prompts.json"
            prompts_path.write_text(json.dumps({**profile, "scenes": scenes}), encoding="utf-8")

            plan = generate_plan(prompts_path, root)

            self.assertEqual(plan["status"], "completed")
            self.assertEqual(plan["backend"], "fake")
            self.assertEqual([scene["status"] for scene in plan["scenes"]], ["downloaded", "downloaded"])
            for scene in plan["scenes"]:
                self.assertRegex(scene["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue((root / scene["target_file"]).is_file())

            final_path = root / "final" / "video.mp4"
            assembled = assemble_verified_clips(
                [root / "clips" / "scene_01.mp4", root / "clips" / "scene_02.mp4"],
                final_path,
                expected_duration=0.8,
                expected_width=1080,
                expected_height=1920,
                expected_clip_sha256={
                    scene["target_file"].split("/")[-1]: scene["sha256"]
                    for scene in plan["scenes"]
                },
                expected_clip_durations={
                    scene["target_file"].split("/")[-1]: scene["duration_seconds"]
                    for scene in plan["scenes"]
                },
            )
            plan_path = root / "clips" / "generation_plan.json"
            summary = compute_package_status(
                root,
                "completed",
                expected_final_sha256=assembled["sha256"],
                expected_plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                expected_final_duration_seconds=assembled["duration_seconds"],
            )

            self.assertTrue(summary["final_video_profile_matches"])
            self.assertTrue(summary["has_final_video"])
            self.assertEqual(summary["package_status"], "final_video_ready")
            self.assertEqual(summary["final_video_probe"]["width"], 1080)
            self.assertEqual(summary["final_video_probe"]["height"], 1920)
            mismatched_evidence = compute_package_status(
                root,
                "completed",
                expected_final_sha256=assembled["sha256"],
                expected_plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                expected_final_duration_seconds=999.0,
            )
            self.assertFalse(mismatched_evidence["has_final_video"])
            zero_duration_evidence = compute_package_status(
                root,
                "completed",
                expected_final_sha256=assembled["sha256"],
                expected_plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                expected_final_duration_seconds=0.0,
            )
            self.assertFalse(zero_duration_evidence["final_evidence_duration_matches"])
            manifest = write_package_manifest(
                root,
                {
                    "id": "manifest-duration",
                    "status": "completed",
                    "final_video_sha256": assembled["sha256"],
                    "final_video_plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                    "final_video_duration_seconds": 999.0,
                },
            )
            self.assertFalse(manifest["has_final_video"])

            short_profile = {
                **profile,
                "width": 1080,
                "height": 1920,
            }
            short_clip = root / "short.mp4"
            self.assertEqual(run_fake_provider("short", 0.1, short_clip, short_profile)["status"], "downloaded")
            (root / "final" / "video.mp4").write_bytes(short_clip.read_bytes())
            short_final_summary = compute_package_status(
                root,
                "completed",
                expected_final_sha256=hashlib.sha256((root / "final" / "video.mp4").read_bytes()).hexdigest(),
                expected_plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            )
            self.assertFalse(short_final_summary["final_video_duration_matches"])
            self.assertFalse(short_final_summary["has_final_video"])

    def test_package_status_rejects_storyboard_total_duration_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"SOLO_STUDIO_VIDEO_PROVIDER": "fake"},
            clear=False,
        ):
            root = Path(tmp)
            profile = {
                "output_profile": "landscape",
                "aspect_ratio": "16:9",
                "resolution": "1920x1080",
                "width": 1920,
                "height": 1080,
            }
            scenes = [
                {"scene_number": 1, "duration_seconds": 0.4, "visual_description": "opening"},
                {"scene_number": 2, "duration_seconds": 0.4, "visual_description": "ending"},
            ]
            (root / "storyboard.json").write_text(
                json.dumps({**profile, "total_duration": 1.4, "scenes": scenes}), encoding="utf-8"
            )
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({**profile, "scenes": scenes}), encoding="utf-8")
            plan = generate_plan(prompts, root)
            final = root / "final" / "video.mp4"
            final.parent.mkdir(parents=True)
            self.assertEqual(run_fake_provider("final", 1.4, final, profile)["status"], "downloaded")
            plan_path = root / "clips" / "generation_plan.json"
            summary = compute_package_status(
                root,
                "completed",
                expected_final_sha256=hashlib.sha256(final.read_bytes()).hexdigest(),
                expected_plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(plan["status"], "completed")
            self.assertFalse(summary["has_final_video"])
            self.assertNotEqual(summary["package_status"], "final_video_ready")

    def test_setup_needed_generation_preserves_existing_clips(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1", "SOLO_STUDIO_VIDEO_PROVIDER": ""},
            clear=False,
        ), patch("engines.generation_agent.shutil.which", return_value=None):
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            existing = clips / "scene_01.mp4"
            original = b"existing-verified-clip"
            existing.write_bytes(original)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "duration_seconds": 1.0}]}), encoding="utf-8")

            plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "setup_needed")
            self.assertEqual(existing.read_bytes(), original)

    def test_provider_failure_preserves_existing_clips(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"SOLO_STUDIO_VIDEO_PROVIDER": "fake"},
            clear=False,
        ), patch(
            "engines.generation_agent.run_fake_provider",
            return_value={"status": "failed", "error": "injected provider failure"},
        ):
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            existing = clips / "scene_01.mp4"
            original = b"existing-verified-clip"
            existing.write_bytes(original)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "duration_seconds": 1.0}]}), encoding="utf-8")

            plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertEqual(existing.read_bytes(), original)
            self.assertFalse(any(path.name.startswith(".clips.generation-") for path in root.iterdir()))

    def test_staging_cleanup_does_not_delete_replacement_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"SOLO_STUDIO_VIDEO_PROVIDER": "fake"},
            clear=False,
        ), patch("engines.generation_agent.run_fake_provider") as provider:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "duration_seconds": 1.0}]}), encoding="utf-8")

            def replace_staging(_prompt, _duration, target, _profile, _deadline):
                original_staging = target.parent
                replacement_name = original_staging.name
                original_staging.rename(root / "original-staging")
                replacement = root / replacement_name
                replacement.mkdir()
                (replacement / "attacker-marker").write_text("must-survive", encoding="utf-8")
                return {"status": "failed", "error": "injected provider failure"}

            provider.side_effect = replace_staging
            plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            replacement = next(root.glob(".clips.generation-*"))
            self.assertEqual((replacement / "attacker-marker").read_text(encoding="utf-8"), "must-survive")
    def test_staged_publication_rejects_noncanonical_name_even_with_matching_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            staged = root / ".staged"
            staged.mkdir()
            artifact = staged / "arbitrary.bin"
            artifact.write_bytes(b"untrusted")
            staged_fd = os.open(staged, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(OSError):
                    _publish_staged_clips_descriptor(
                        staged,
                        clips,
                        staged_fd,
                        expected_hashes={"arbitrary.bin": hashlib.sha256(b"untrusted").hexdigest()},
                    )
            finally:
                os.close(staged_fd)
            self.assertEqual(artifact.read_bytes(), b"untrusted")

    def test_staged_publication_validates_before_purging_canonical_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            old = clips / "old.mp4"
            old.write_bytes(b"old")
            staged = root / ".staged"
            staged.mkdir()
            (staged / "scene_01.mp4").write_bytes(b"new")
            os.mkfifo(staged / "invalid")
            staged_fd = os.open(staged, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(OSError):
                    _publish_staged_clips_descriptor(
                        staged,
                        clips,
                        staged_fd,
                        expected_hashes={"scene_01.mp4": hashlib.sha256(b"new").hexdigest()},
                    )
            finally:
                os.close(staged_fd)
            self.assertEqual(old.read_bytes(), b"old")
            self.assertFalse((clips / "new.mp4").exists())

    def test_staged_publication_rejects_content_hash_substitution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            old = clips / "old.mp4"
            old.write_bytes(b"old")
            staged = root / ".staged"
            staged.mkdir()
            staged_file = staged / "scene_01.mp4"
            staged_file.write_bytes(b"untrusted-content")
            staged_fd = os.open(staged, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(OSError):
                    _publish_staged_clips_descriptor(
                        staged,
                        clips,
                        staged_fd,
                        expected_hashes={"scene_01.mp4": hashlib.sha256(b"trusted-content").hexdigest()},
                    )
            finally:
                os.close(staged_fd)
            self.assertEqual(old.read_bytes(), b"old")
            self.assertFalse((clips / "scene_01.mp4").exists())

    def test_staged_publication_materializes_source_before_source_mutation(self):
        real_fsync = os.fsync
        with tempfile.TemporaryDirectory() as tmp, patch("engines.generation_agent.os.fsync") as fsync:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            staged = root / ".staged"
            staged.mkdir()
            source = staged / "scene_01.mp4"
            source.write_bytes(b"trusted")
            staged_fd = os.open(staged, os.O_RDONLY | os.O_DIRECTORY)
            mutated = False

            def mutate_source_after_copy(fd):
                nonlocal mutated
                result = real_fsync(fd)
                if not mutated:
                    source.write_bytes(b"attacker")
                    mutated = True
                return result

            fsync.side_effect = mutate_source_after_copy
            try:
                _publish_staged_clips_descriptor(
                    staged,
                    clips,
                    staged_fd,
                    expected_hashes={"scene_01.mp4": hashlib.sha256(b"trusted").hexdigest()},
                )
            finally:
                os.close(staged_fd)
            self.assertTrue(mutated)
            self.assertEqual((clips / "scene_01.mp4").read_bytes(), b"trusted")

    def test_publication_rejects_replacement_after_parent_fsync(self):
        real_fsync = os.fsync
        with tempfile.TemporaryDirectory() as tmp, patch("engines.generation_agent._open_directory_no_follow") as open_dir, patch(
            "engines.generation_agent.os.fsync"
        ) as fsync:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            (clips / "old.mp4").write_bytes(b"old")
            staged = root / ".staged"
            staged.mkdir()
            (staged / "scene_01.mp4").write_bytes(b"new")
            staged_fd = os.open(staged, os.O_RDONLY | os.O_DIRECTORY)
            real_open = __import__("package_utils")._open_directory_no_follow
            parent_fd = None
            swapped = False

            def capture_parent(path, create=False):
                nonlocal parent_fd
                parent_fd = real_open(path, create=create)
                return parent_fd

            def fsync_with_replacement(fd):
                nonlocal swapped
                real_fsync(fd)
                if parent_fd is not None and fd == parent_fd and not swapped:
                    clips.rename(root / "old-clips")
                    clips.mkdir()
                    (clips / "attacker-marker").write_text("must-not-remain", encoding="utf-8")
                    swapped = True

            open_dir.side_effect = capture_parent
            fsync.side_effect = fsync_with_replacement
            try:
                with self.assertRaises(OSError):
                    _publish_staged_clips_descriptor(
                        staged,
                        clips,
                        staged_fd,
                        expected_hashes={"scene_01.mp4": hashlib.sha256(b"new").hexdigest()},
                    )
            finally:
                os.close(staged_fd)
            self.assertTrue(swapped)
            self.assertFalse(clips.exists())
            self.assertTrue((root / "old-clips").exists())

    def test_publication_rejects_clip_mutation_during_old_directory_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            (clips / "old.mp4").write_bytes(b"old")
            staged = root / ".staged"
            staged.mkdir()
            (staged / "scene_01.mp4").write_bytes(b"trusted")
            staged_fd = os.open(staged, os.O_RDONLY | os.O_DIRECTORY)
            real_remove_tree = __import__("engines.generation_agent", fromlist=["_remove_tree_at"])._remove_tree_at

            def remove_then_mutate(parent_fd, name, expected_inode=None, *, deadline=None):
                result = real_remove_tree(parent_fd, name, expected_inode, deadline=deadline)
                (clips / "scene_01.mp4").write_bytes(b"changed")
                return result

            try:
                with patch("engines.generation_agent._remove_tree_at", side_effect=remove_then_mutate):
                    with self.assertRaises(OSError):
                        _publish_staged_clips_descriptor(
                            staged,
                            clips,
                            staged_fd,
                            expected_hashes={"scene_01.mp4": hashlib.sha256(b"trusted").hexdigest()},
                        )
            finally:
                os.close(staged_fd)
            self.assertEqual((clips / "scene_01.mp4").read_bytes(), b"changed")

    def test_provider_dns_deadline_returns_controlled_failure(self):
        def slow_resolver(*_args, **_kwargs):
            time.sleep(0.2)
            return []

        started = time.monotonic()
        with patch("engines.generation_agent.socket.getaddrinfo", side_effect=slow_resolver):
            with self.assertRaises(TimeoutError):
                _public_provider_ip("https://example.test", deadline=started + 0.05)
        self.assertLess(time.monotonic() - started, 0.15)

    def test_expired_higgsfield_deadline_returns_controlled_failure(self):
        with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}, clear=False):
            result = run_higgsfield(
                "prompt",
                1.0,
                Path("/tmp/unused-provider-clip.mp4"),
                "seedance_2_0",
                deadline=time.monotonic() - 1,
            )
        self.assertEqual(result["status"], "failed")
        self.assertIn("deadline", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
