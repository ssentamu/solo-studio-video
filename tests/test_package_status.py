import json
import io
import threading
import re
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from package_utils import _probe_media, atomic_write_json, clear_generated_artifacts, compute_package_status, read_json_object, update_json_file, write_package_manifest
from engines.generation_agent import generate_plan, run_higgsfield


class PackageStatusTests(unittest.TestCase):
    def test_atomic_json_writes_use_unique_temp_files_for_concurrent_writers(self):
        from package_utils import atomic_write_json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            errors = []

            def write(index):
                try:
                    atomic_write_json(path, {"writer": index})
                except Exception as exc:  # pragma: no cover - assertion captures the concrete failure
                    errors.append(exc)

            threads = [threading.Thread(target=write, args=(index,)) for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertIn("writer", json.loads(path.read_text()))

    def test_atomic_jobs_write_normalizes_insecure_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text("{}")
            path.chmod(0o666)
            atomic_write_json(path, {"safe": True})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o660)

    def test_non_mp4_infinite_duration_is_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voiceover.mp3"
            path.write_bytes(b"audio")
            with patch("package_utils.shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "package_utils.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [{"codec_type": "audio"}], "format": {"duration": "Infinity"}}), stderr=""),
            ):
                result = _probe_media(path)
            self.assertFalse(result["valid"])
            self.assertIn("finite", result["error"])

    def test_clear_generated_artifacts_removes_stale_media_but_preserves_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "brief.yaml").write_text("topic: keep me\n")
            (root / "visuals").mkdir()
            (root / "visuals" / "scene_01.png").write_bytes(b"old image")
            (root / "clips").mkdir()
            (root / "clips" / "scene_01.mp4").write_bytes(b"old clip")
            (root / "package_manifest.json").write_text("{}")

            removed = clear_generated_artifacts(root)

            self.assertTrue((root / "brief.yaml").is_file())
            self.assertFalse((root / "visuals").exists())
            self.assertFalse((root / "clips").exists())
            self.assertFalse((root / "package_manifest.json").exists())
            self.assertIn("visuals", removed)
            self.assertIn("clips", removed)

    def test_update_json_file_serializes_read_modify_write_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text(json.dumps({"seed": {"count": 0}}))

            def add_job(idx: int):
                def updater(jobs: dict) -> dict:
                    jobs[f"job-{idx}"] = {"id": f"job-{idx}"}
                    return jobs

                update_json_file(path, updater)

            threads = [threading.Thread(target=add_job, args=(idx,)) for idx in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            saved = json.loads(path.read_text())
            self.assertEqual(len([key for key in saved if key.startswith("job-")]), 20)

    def test_update_json_file_refuses_to_overwrite_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text('{"broken":')

            with self.assertRaises(ValueError):
                update_json_file(path, lambda jobs: {**jobs, "new": {"id": "new"}})

            self.assertEqual(path.read_text(), '{"broken":')

    def test_read_json_object_refuses_corrupt_or_non_object_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text('{"broken":')
            with self.assertRaises(ValueError):
                read_json_object(path)

            path.write_text('[{"id": "not-a-job-map"}]')
            with self.assertRaises(ValueError):
                read_json_object(path)

    def test_editor_package_status_requires_editor_artifacts_not_real_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "audio").mkdir()
            artifacts = {
                "creative_brief.json": "{}",
                "script.txt": "script",
                "storyboard.json": json.dumps({"scenes": [{"scene_number": 1}], "total_duration": 6}),
                "video_prompts.json": json.dumps({"scenes": []}),
                "audio/voiceover_script.txt": "hello",
                "music_prompt.txt": "music",
                "captions.srt": "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
                "assembly_manifest.json": "{}",
                "timeline.fcpxml": "<fcpxml></fcpxml>",
            }
            for rel, content in artifacts.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            status = compute_package_status(root, "completed")

            self.assertEqual(status["package_status"], "editor_package")
            self.assertFalse(status["has_clips"])
            self.assertFalse(status["has_final_video"])

    def test_manifest_records_prompt_only_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "video_prompts.json").write_text(json.dumps({"scenes": []}))

            manifest = write_package_manifest(root, {"id": "job1", "status": "completed"})

            self.assertEqual(manifest["package_status"], "prompt_package_only")
            self.assertEqual(manifest["job"]["package_status"], "prompt_package_only")
            self.assertFalse(manifest["job"]["has_final_video"])
            self.assertTrue((root / "package_manifest.json").is_file())
            saved = json.loads((root / "package_manifest.json").read_text())
            self.assertEqual(saved["package_status"], "prompt_package_only")
            self.assertEqual(saved["job"]["package_status"], saved["package_status"])

    def test_zero_byte_visual_does_not_count_as_visuals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "visuals").mkdir()
            (root / "visuals" / "scene_01.png").write_bytes(b"")

            status = compute_package_status(root, "completed")

            self.assertFalse(status["has_visuals"])

    def test_corrupt_png_visual_does_not_count_as_visuals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "visuals").mkdir()
            (root / "visuals" / "scene_01.png").write_bytes(b"not really a png")

            status = compute_package_status(root, "completed")

            self.assertFalse(status["has_visuals"])

    def test_ffprobe_timeout_is_nonfatal_and_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "final" / "video.mp4"
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")

            import subprocess
            with patch("package_utils.shutil.which", return_value="ffprobe"), patch(
                "package_utils.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 15)
            ):
                status = compute_package_status(root, "completed")

            self.assertFalse(status["has_final_video"])
            self.assertEqual(status["package_status"], "not_started")
            self.assertEqual(status["final_video_probe"]["error"], "ffprobe timed out")

    def test_malformed_storyboard_is_reported_not_crashed_or_treated_as_editor_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "audio").mkdir()
            fixtures = {
                "creative_brief.json": "{}",
                "script.txt": "script",
                "storyboard.json": "[]",
                "video_prompts.json": json.dumps({"scenes": []}),
                "audio/voiceover_script.txt": "voiceover",
                "music_prompt.txt": "music",
                "captions.srt": "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
                "assembly_manifest.json": "{}",
                "timeline.fcpxml": "<fcpxml></fcpxml>",
            }
            for rel, content in fixtures.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            status = compute_package_status(root, "completed")

            self.assertEqual(status["expected_scenes"], 0)
            self.assertFalse(status["artifacts"]["storyboard"])
            self.assertIn("storyboard.json must be an object", status["artifact_errors"][0])
            self.assertNotEqual(status["package_status"], "editor_package")

            (root / "storyboard.json").write_text(json.dumps({"scenes": []}))
            status = compute_package_status(root, "completed")
            self.assertFalse(status["artifacts"]["storyboard"])
            self.assertNotEqual(status["package_status"], "editor_package")

    def test_clips_generated_requires_exact_expected_scene_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "storyboard.json").write_text(json.dumps({"scenes": [{}, {}]}))
            (root / "clips").mkdir()
            (root / "clips" / "scene_01.mp4").write_bytes(b"valid enough for mocked ffprobe")
            (root / "clips" / "scene_99.mp4").write_bytes(b"valid enough for mocked ffprobe")

            def fake_probe(path):
                if path.name.startswith("scene_"):
                    return {"path": str(path), "exists": True, "size_bytes": 1, "ffprobe_checked": True, "valid": True, "duration_seconds": 1.0}
                return {"path": str(path), "exists": False, "size_bytes": 0, "ffprobe_checked": False, "valid": False}

            with patch("package_utils._probe_media", side_effect=fake_probe):
                status = compute_package_status(root, "completed")

            self.assertEqual(status["verified_clip_scene_numbers"], [1, 99])
            self.assertEqual(status["missing_clip_scene_numbers"], [2])
            self.assertEqual(status["extra_clip_scene_numbers"], [99])
            self.assertTrue(status["has_partial_clips"])
            self.assertFalse(status["has_clips"])
            self.assertNotEqual(status["package_status"], "clips_generated")

    def test_case_variant_clip_filenames_block_clip_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "storyboard.json").write_text(json.dumps({"scenes": [{}]}))
            (root / "clips").mkdir()
            (root / "clips" / "scene_01.mp4").write_bytes(b"valid enough for mocked ffprobe")
            (root / "clips" / "scene_99.MP4").write_bytes(b"valid enough for mocked ffprobe")

            def fake_probe(path):
                if path.name.startswith("scene_"):
                    return {"path": str(path), "exists": True, "size_bytes": 1, "ffprobe_checked": True, "valid": True, "duration_seconds": 1.0}
                return {"path": str(path), "exists": False, "size_bytes": 0, "ffprobe_checked": False, "valid": False}

            with patch("package_utils._probe_media", side_effect=fake_probe):
                status = compute_package_status(root, "completed")

            self.assertEqual(status["verified_clip_scene_numbers"], [1])
            self.assertEqual(status["invalid_clip_files"], ["scene_99.MP4"])
            self.assertEqual(status["extra_clip_scene_numbers"], [])
            self.assertEqual(status["missing_clip_scene_numbers"], [])
            self.assertTrue(status["has_partial_clips"])
            self.assertFalse(status["has_clips"])
            self.assertNotEqual(status["package_status"], "clips_generated")

    def test_extra_verified_clip_prevents_clips_generated_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "storyboard.json").write_text(json.dumps({"scenes": [{}, {}]}))
            (root / "clips").mkdir()
            for scene_number in (1, 2, 99):
                (root / "clips" / f"scene_{scene_number:02d}.mp4").write_bytes(b"verified")

            def fake_probe(path):
                if path.suffix == ".mp4":
                    return {"path": str(path), "exists": True, "size_bytes": 1, "ffprobe_checked": True, "valid": True, "duration_seconds": 1.0}
                return {"path": str(path), "exists": False, "size_bytes": 0, "ffprobe_checked": False, "valid": False}

            with patch("package_utils._probe_media", side_effect=fake_probe):
                status = compute_package_status(root, "completed")

            self.assertEqual(status["verified_clip_scene_numbers"], [1, 2, 99])
            self.assertEqual(status["extra_clip_scene_numbers"], [99])
            self.assertFalse(status["has_clips"])
            self.assertNotEqual(status["package_status"], "clips_generated")


class GenerationAgentTests(unittest.TestCase):
    def test_generation_agent_rejects_malformed_json_and_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed = root / "malformed.json"
            malformed.write_text("not json")
            self.assertEqual(generate_plan(malformed, root)["status"], "failed")

            prompts = root / "durations.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "duration_seconds": "NaN"}]}))
            plan = generate_plan(prompts, root)
            self.assertEqual(plan["status"], "failed")
            self.assertIn("finite positive", plan["reason"])

            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "duration_seconds": True}]}))
            plan = generate_plan(prompts, root)
            self.assertEqual(plan["status"], "failed")
            self.assertIn("finite positive", plan["reason"])

            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "seedance_prompt": 42}]}))
            plan = generate_plan(prompts, root)
            self.assertEqual(plan["status"], "failed")
            self.assertIn("must be strings", plan["reason"])

    def test_worker_and_api_share_configured_jobs_file(self):
        worker_source = (ROOT / "worker.py").read_text()
        api_source = (ROOT / "api.py").read_text()
        deploy_source = (ROOT / "deploy-traefik.sh").read_text()
        self.assertIn("SOLO_STUDIO_JOBS_FILE", worker_source)
        self.assertIn("SOLO_STUDIO_JOBS_FILE", api_source)
        self.assertIn("SOLO_STUDIO_JOBS_FILE=/app/state/jobs.json", deploy_source)

    def test_generation_agent_rejects_failed_array_provider_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "engines.generation_agent.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([{"status": "failed", "result_url": "https://provider.example/video.mp4"}]),
                    stderr="",
                ),
            ), patch("engines.generation_agent._open_provider_url") as open_url:
                result = run_higgsfield("safe prompt", 5, Path(tmp) / "clip.mp4", "model")
            self.assertEqual(result["status"], "failed")
            open_url.assert_not_called()

    def test_generation_agent_writes_dry_run_plan_without_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clips").mkdir()
            (root / "clips" / "scene_01.mp4").write_bytes(b"stale clip")
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({
                "scenes": [
                    {
                        "scene_number": 1,
                        "duration_seconds": 6,
                        "runway_prompt": "A cinematic product shot.",
                        "seedance_prompt": "A Seedance-specific product shot.",
                        "kling_prompt": "Scene: A cinematic product shot. Camera movement: slow push.",
                        "transition": "cut",
                    }
                ]
            }))

            plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "dry_run")
            self.assertEqual(plan["total_scenes"], 1)
            self.assertTrue((root / "clips" / "generation_plan.json").is_file())
            self.assertFalse((root / "clips" / "scene_01.mp4").exists())
            self.assertIn("SOLO_STUDIO_ENABLE_HIGGSFIELD", plan["setup_needed"])
            self.assertEqual(plan["scenes"][0]["source_prompts"]["seedance"], "A Seedance-specific product shot.")

    def test_generation_agent_accepts_documented_array_provider_response(self):
        from engines.generation_agent import _provider_url

        payload = [{"status": "completed", "result": {"video_url": "https://provider.example/scene.mp4"}}]

        self.assertEqual(_provider_url(payload), "https://provider.example/scene.mp4")

    def test_generation_agent_rejects_malformed_scene_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1}, {"scene_number": 1}]}))

            plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertIn("unique positive integer", plan["reason"])

            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1}, {"scene_number": 3}]}))
            plan = generate_plan(prompts, root)
            self.assertEqual(plan["status"], "failed")
            self.assertIn("contiguous", plan["reason"])

    def test_generation_agent_clears_stale_clips_before_invalid_input_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            stale = clips / "scene_01.mp4"
            stale.write_bytes(b"stale")
            plan = generate_plan(root / "missing-video-prompts.json", root)
            self.assertEqual(plan["status"], "failed")
            self.assertTrue(stale.exists())

    def test_generation_agent_requires_video_stream_and_finite_duration(self):
        from engines.generation_agent import _verify_clip

        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "scene.mp4"
            clip.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")
            with patch("engines.generation_agent.shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "engines.generation_agent.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"streams": [], "format": {"duration": "1.0"}}),
                    stderr="",
                ),
            ):
                valid, reason = _verify_clip(clip)
            self.assertFalse(valid)
            self.assertIn("no video stream", reason or "")

            with patch("engines.generation_agent.shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "engines.generation_agent.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "NaN"}}),
                    stderr="",
                ),
            ):
                valid, reason = _verify_clip(clip)
            self.assertFalse(valid)
            self.assertIn("positive", reason or "")

    def test_generation_agent_real_higgsfield_mode_downloads_verified_scene_without_log_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({
                "scenes": [
                    {
                        "scene_number": 1,
                        "duration_seconds": 6,
                        "seedance_prompt": "A Seedance-specific product shot.",
                    }
                ]
            }))

            class FakeResponse:
                def __init__(self):
                    self._chunks = [b"fake mp4 bytes", b""]

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _size=-1):
                    return self._chunks.pop(0)

            provider_stdout = json.dumps({
                "result_url": "https://cdn.example.test/scene-01.mp4",
                "debug": "provider-token-should-not-be-persisted",
            })

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/local/bin/higgsfield"
            ), patch("engines.generation_agent.subprocess.run") as run, patch(
                "engines.generation_agent._open_provider_url", return_value=FakeResponse()
            ), patch(
                "engines.generation_agent._verify_clip", return_value=(True, None)
            ):
                run.return_value.returncode = 0
                run.return_value.stdout = provider_stdout
                run.return_value.stderr = "stderr-token-should-not-be-persisted"

                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "completed")
            self.assertEqual(plan["scenes"][0]["status"], "downloaded")
            self.assertEqual((root / "clips" / "scene_01.mp4").read_bytes(), b"fake mp4 bytes")
            serialized = json.dumps(plan)
            self.assertNotIn("provider-token-should-not-be-persisted", serialized)
            self.assertNotIn("stderr-token-should-not-be-persisted", serialized)
            self.assertNotIn("cdn.example.test", serialized)

    def test_generation_agent_rejects_invalid_provider_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "seedance_prompt": "scene"}]}))

            class FakeResponse:
                def __init__(self):
                    self._chunks = [b"not a valid mp4 artifact", b""]

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _size=-1):
                    return self._chunks.pop(0)

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which",
                side_effect=["/usr/bin/higgsfield", "/usr/bin/ffprobe"],
            ), patch(
                "engines.generation_agent.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"result_url": "https://cdn.example.test/bad"}),
                    stderr="",
                ),
            ), patch("engines.generation_agent._open_provider_url", return_value=FakeResponse()):
                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertEqual(plan["scenes"][0]["status"], "failed")
            self.assertFalse((root / "clips" / "scene_01.mp4").exists())
            self.assertIn("non-MP4", plan["scenes"][0]["error"])

    def test_generation_agent_rejects_empty_provider_download_without_publishing_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "seedance_prompt": "scene"}]}))

            class EmptyResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _size=-1):
                    return b""

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/bin/higgsfield"
            ), patch(
                "engines.generation_agent.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"result_url": "https://cdn.example.test/empty.mp4"}),
                    stderr="",
                ),
            ), patch("engines.generation_agent._open_provider_url", return_value=EmptyResponse()):
                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertEqual(plan["scenes"][0]["status"], "failed")
            self.assertFalse((root / "clips" / "scene_01.mp4").exists())
            self.assertIn("Video download failed", plan["scenes"][0]["error"])

    def test_generation_agent_missing_https_result_url_fails_without_provider_url_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "seedance_prompt": "scene"}]}))

            provider_stdout = json.dumps({
                "result_url": "http://internal.example.test/scene.mp4",
                "debug": "provider-debug-token-should-not-leak",
            })
            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/bin/higgsfield"
            ), patch("engines.generation_agent.subprocess.run") as run, patch(
                "engines.generation_agent._open_provider_url"
            ) as opener:
                run.return_value.returncode = 0
                run.return_value.stdout = provider_stdout
                run.return_value.stderr = "stderr-token-should-not-leak"

                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertIn("without an HTTPS video URL", plan["scenes"][0]["error"])
            self.assertFalse((root / "clips" / "scene_01.mp4").exists())
            opener.assert_not_called()
            serialized = json.dumps(plan)
            self.assertNotIn("internal.example.test", serialized)
            self.assertNotIn("provider-debug-token-should-not-leak", serialized)
            self.assertNotIn("stderr-token-should-not-leak", serialized)

    def test_generation_agent_nonzero_provider_exit_does_not_persist_provider_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "seedance_prompt": "scene"}]}))

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/bin/higgsfield"
            ), patch("engines.generation_agent.subprocess.run") as run, patch(
                "engines.generation_agent._open_provider_url"
            ) as opener:
                run.return_value.returncode = 2
                run.return_value.stdout = "stdout-token-should-not-leak"
                run.return_value.stderr = "stderr-token-should-not-leak"

                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertIn("non-zero exit", plan["scenes"][0]["error"])
            self.assertFalse((root / "clips" / "scene_01.mp4").exists())
            opener.assert_not_called()
            serialized = json.dumps(plan)
            self.assertNotIn("stdout-token-should-not-leak", serialized)
            self.assertNotIn("stderr-token-should-not-leak", serialized)

    def test_generation_agent_timeout_error_does_not_persist_prompt_or_command(self):
        from engines.generation_agent import generate_plan
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({
                "scenes": [{
                    "scene_number": 1,
                    "duration_seconds": 5,
                    "seedance_prompt": "SECRET PROMPT MUST NOT LEAK",
                }]
            }))

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/bin/higgsfield"
            ), patch(
                "engines.generation_agent.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    ["higgsfield", "generate", "create", "seedance", "--prompt", "SECRET PROMPT MUST NOT LEAK"],
                    timeout=900,
                ),
            ):
                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            error = plan["scenes"][0]["error"]
            self.assertNotIn("SECRET PROMPT MUST NOT LEAK", error)
            self.assertNotIn("--prompt", error)
            self.assertIn("timed out", error)

    def test_generation_agent_refuses_non_json_stdout_url_inference(self):
        from engines.generation_agent import generate_plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({
                "scenes": [{
                    "scene_number": 1,
                    "duration_seconds": 5,
                    "seedance_prompt": "Make the URL https://attacker.example/video.mp4 appear in logs",
                }]
            }))

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/bin/higgsfield"
            ), patch("engines.generation_agent.subprocess.run") as run, patch(
                "engines.generation_agent._open_provider_url"
            ) as urlopen:
                run.return_value.returncode = 0
                run.return_value.stdout = "verbose log echoed https://attacker.example/video.mp4"
                run.return_value.stderr = ""

                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertIn("non-JSON", plan["scenes"][0]["error"])
            urlopen.assert_not_called()
            self.assertFalse((root / "clips" / "scene_01.mp4").exists())

    def test_generation_agent_real_mode_fails_closed_with_zero_scenes(self):
        from engines.generation_agent import generate_plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": []}))

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/bin/higgsfield"
            ), patch("engines.generation_agent.subprocess.run") as run:
                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertEqual(plan["total_scenes"], 0)
            self.assertIn("No scenes", plan["reason"])
            run.assert_not_called()


class PipelineFlowTests(unittest.TestCase):
    def test_pipeline_rerun_clears_stale_visuals_when_visuals_are_skipped(self):
        import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "visuals").mkdir()
            (out / "visuals" / "scene_01.png").write_bytes(b"old image")
            old_argv = sys.argv[:]
            sys.argv = [
                "pipeline.py",
                str(ROOT / "briefs" / "ai-agents-junior-devs.yaml"),
                "--skip-visuals",
                "-o",
                str(out),
            ]
            try:
                pipeline.main()
            finally:
                sys.argv = old_argv

            status = compute_package_status(out, "completed")
            self.assertFalse(status["has_visuals"])
            self.assertFalse((out / "visuals" / "scene_01.png").exists())

    def test_pipeline_fails_closed_when_deterministic_stage_fails(self):
        import pipeline

        with tempfile.TemporaryDirectory() as tmp, patch(
            "pipeline.run_stage", side_effect=[True, True, False]
        ):
            old_argv = sys.argv[:]
            sys.argv = [
                "pipeline.py",
                str(ROOT / "briefs" / "ai-agents-junior-devs.yaml"),
                "--skip-visuals",
                "-o",
                tmp,
            ]
            try:
                with self.assertRaises(SystemExit) as exc:
                    pipeline.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(exc.exception.code, 1)
            manifest = Path(tmp) / "package_manifest.json"
            self.assertTrue(manifest.is_file())
            saved = json.loads(manifest.read_text())
            self.assertEqual(saved["job"]["status"], "failed")
            self.assertEqual(saved["package_status"], "failed")

    def test_pipeline_fails_closed_when_success_manifest_write_fails(self):
        import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            def fake_run_stage(name, *_args):
                if name == "4. Production Agent":
                    (Path(tmp) / "video_prompts.json").write_text(json.dumps({"scenes": []}))
                return True

            patchers = patch("pipeline.run_stage", side_effect=fake_run_stage), patch(
            "pipeline._generate_thumbnail_prompt", return_value=None
            ), patch("pipeline.write_package_manifest", side_effect=OSError("disk full"))
            old_argv = sys.argv[:]
            sys.argv = [
                "pipeline.py",
                str(ROOT / "briefs" / "ai-agents-junior-devs.yaml"),
                "--skip-visuals",
                "-o",
                tmp,
            ]
            with patchers[0], patchers[1], patchers[2]:
                try:
                    with self.assertRaises(SystemExit) as exc:
                        pipeline.main()
                finally:
                    sys.argv = old_argv

            self.assertEqual(exc.exception.code, 1)


class WorkerFlowTests(unittest.TestCase):
    def test_worker_media_helpers_do_not_claim_prompt_only_artifacts_are_real_media(self):
        import worker

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "audio").mkdir(parents=True)
            (root / "visual_prompts.json").write_text(json.dumps({"prompts": [{"scene_number": 1}]}))
            (root / "audio" / "voiceover_script.txt").write_text("voiceover text")

            self.assertFalse(worker._generate_visuals(root, {"scenes": [{"scene_number": 1}]}))
            self.assertFalse(worker._generate_voiceover(root, {"scenes": []}))
            self.assertFalse((root / "audio" / "voiceover.mp3").exists())

            self.assertFalse(worker._generate_visuals(Path(tmp) / "missing-visuals", {"scenes": []}))
            self.assertFalse(worker._generate_voiceover(Path(tmp) / "missing-voiceover", {"scenes": []}))

    def test_worker_early_stage_failures_write_package_manifest(self):
        import worker

        old_jobs_file = worker.JOBS_FILE
        old_output_root = worker.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker.JOBS_FILE = root / "jobs.json"
            worker.OUTPUT_ROOT = root / "output"
            job = {"id": "research-fail", "topic": "Research fails", "status": "queued"}
            update_json_file(worker.JOBS_FILE, lambda _jobs: {job["id"]: job})
            try:
                with patch("worker.run_stage", return_value=False):
                    worker.process_job(job["id"], job)
            finally:
                worker.JOBS_FILE = old_jobs_file
                worker.OUTPUT_ROOT = old_output_root

            manifest = root / "output" / job["id"] / "package_manifest.json"
            self.assertTrue(manifest.is_file())
            saved = json.loads(manifest.read_text())
            self.assertEqual(saved["job"]["status"], "failed")
            self.assertEqual(saved["package_status"], "failed")

    def test_worker_failure_manifest_uses_latest_persisted_job_snapshot(self):
        import worker

        old_jobs_file = worker.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker.JOBS_FILE = root / "jobs.json"
            original = {
                "id": "latest-snapshot",
                "topic": "Original job",
                "status": "queued",
                "stage": "waiting",
                "progress": 0.0,
                "package_status": "not_started",
            }
            persisted = {
                **original,
                "status": "running",
                "stage": "script",
                "progress": 0.14,
                "format": "short",
                "chapters": 3,
                "scenes": 5,
            }
            update_json_file(worker.JOBS_FILE, lambda _jobs: {original["id"]: persisted})
            try:
                worker._fail_job(original["id"], root / "output" / original["id"], original, "boom")
            finally:
                worker.JOBS_FILE = old_jobs_file

            saved = json.loads((root / "output" / original["id"] / "package_manifest.json").read_text())
            self.assertEqual(saved["job"]["status"], "failed")
            self.assertEqual(saved["job"]["stage"], "script")
            self.assertEqual(saved["job"]["progress"], 0.14)
            self.assertEqual(saved["job"]["format"], "short")
            self.assertEqual(saved["job"]["scenes"], 5)
            self.assertEqual(saved["job"]["package_status"], saved["package_status"])

    def test_worker_stage_timeout_returns_false_instead_of_crashing(self):
        import subprocess
        import worker

        with patch("worker.subprocess.run", side_effect=subprocess.TimeoutExpired("stage", 300)):
            self.assertFalse(worker.run_stage("job-timeout", "Slow Stage", "script_agent.py", "arg"))

    def test_worker_failure_finalizer_does_not_recurse_on_corrupt_jobs_file(self):
        import worker

        old_jobs_file = worker.JOBS_FILE
        old_output_root = worker.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker.JOBS_FILE = root / "jobs.json"
            worker.OUTPUT_ROOT = root / "output"
            worker.JOBS_FILE.write_text('{"broken":')
            try:
                worker.process_job("corrupt-job", {"id": "corrupt-job", "topic": "Bad state"})
            finally:
                worker.JOBS_FILE = old_jobs_file
                worker.OUTPUT_ROOT = old_output_root

            manifest = root / "output" / "corrupt-job" / "package_manifest.json"
            self.assertTrue(manifest.is_file())
            self.assertEqual((root / "jobs.json").read_text(), '{"broken":')

    def test_worker_failure_finalizer_persists_fallback_when_manifest_write_fails(self):
        import worker

        old_jobs_file = worker.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker.JOBS_FILE = root / "jobs.json"
            job = {"id": "manifest-fail", "topic": "Manifest write fails", "status": "queued"}
            update_json_file(worker.JOBS_FILE, lambda _jobs: {job["id"]: job})
            try:
                with patch("worker.write_package_manifest", side_effect=OSError("disk full")):
                    worker._fail_job(job["id"], root / "output" / job["id"], job, "boom")
            finally:
                worker.JOBS_FILE = old_jobs_file

            saved = json.loads((root / "jobs.json").read_text())[job["id"]]
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["package_status"], "failed")
            self.assertFalse(saved["has_final_video"])

    def test_worker_failure_finalizer_swallows_jobs_write_oserror(self):
        import worker

        with tempfile.TemporaryDirectory() as tmp:
            job = {"id": "jobs-write-fail", "topic": "Jobs write fails", "status": "queued"}
            with patch("worker.update_job", side_effect=OSError("lock file permission denied")):
                worker._fail_job(job["id"], Path(tmp) / job["id"], job, "boom")

            self.assertTrue((Path(tmp) / job["id"] / "package_manifest.json").is_file())

    def test_worker_poll_loop_survives_jobs_store_oserror(self):
        import worker

        with patch("worker.load_jobs", side_effect=OSError("lock file permission denied")), patch(
            "worker.time.sleep", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                worker.main()


class FrontendContractTests(unittest.TestCase):
    def test_pipeline_step_placeholders_match_stage_array(self):
        html = (ROOT / "frontend" / "index.html").read_text()
        block = re.search(
            r'<div class="pipeline" id="pipeline-steps">(.*?)\n      </div>\n\n      <div class="stats">',
            html,
            re.S,
        )
        stages = re.search(r"const stages = \[(.*?)\];", html, re.S)

        if block is None or stages is None:
            self.fail("frontend pipeline DOM block or stage array not found")
        placeholders = block.group(1).count('class="pipeline-step"')
        stage_count = len(re.findall(r"'[^']+'", stages.group(1)))

        self.assertEqual(placeholders, stage_count)

    def test_download_artifact_pills_are_derived_from_artifact_summary(self):
        html = (ROOT / "frontend" / "index.html").read_text()
        function = re.search(r"function showDownloadView\(job\) \{(.*?)\n\}\n\n// ── Recent jobs", html, re.S)
        if function is None:
            self.fail("showDownloadView function not found")
        body = function.group(1)

        self.assertIn("const artifacts = job.artifact_summary || {}", body)
        self.assertNotIn("ok: true", body)
        self.assertIn("artifacts.creative_brief", body)
        self.assertIn("artifacts.video_prompts", body)
        self.assertIn("artifacts.assembly_manifest", body)
        self.assertIn("{ label: 'Scene Images', ok: job.has_visuals }", body)
        self.assertNotIn("job.has_visuals || artifacts.visual_prompts", body)
        self.assertIn("Available artifacts are marked below", html)

    def test_template_loader_has_no_debug_banner_or_step_diagnostics(self):
        html = (ROOT / "frontend" / "index.html").read_text()

        self.assertNotIn("JS init section reached OK", html)
        self.assertNotIn("Loading templates (step", html)
        self.assertIn("Loading templates...", html)
        self.assertNotIn("escapeJsAttr", html)
        self.assertNotIn("onclick=\"viewJob('${j.id}')\"", html)
        self.assertNotIn("onclick=\"selectTemplate", html)
        self.assertNotIn("onclick=\"quickStart", html)
        self.assertIn("safeClassToken", html)
        self.assertIn("addEventListener('click'", html)

    def test_frontend_uses_cookie_session_not_browser_token_storage_for_protected_job_routes(self):
        html = (ROOT / "frontend" / "index.html").read_text()

        self.assertIn("id=\"api-token\"", html)
        self.assertIn("function loginWithApiToken", html)
        self.assertIn("function protectedFetch", html)
        self.assertIn("credentials: 'same-origin'", html)
        self.assertIn("/auth/session", html)
        self.assertNotIn("API_TOKEN_STORAGE_KEY", html)
        self.assertNotIn("headers.set('Authorization', 'Bearer ' + token)", html)
        self.assertNotIn("sessionStorage", html)
        self.assertNotIn("localStorage", html)
        self.assertIn("protectedFetch('/jobs'", html)
        self.assertIn("protectedFetch('/jobs/' + currentJobId)", html)
        self.assertIn("protectedFetch('/jobs?limit=10')", html)
        self.assertIn("protectedFetch('/jobs/from-template/'", html)
        self.assertIn("protectedFetch('/jobs/' + encodeURIComponent(jobId) + '/download')", html)
        self.assertIn("filenameFromDisposition", html)
        self.assertIn("res.headers.get('Content-Disposition')", html)
        self.assertNotIn("id=\"download-link\"", html)


class ApiPackageStatusTests(unittest.TestCase):
    def test_health_endpoint_is_available_for_deploy_smoke(self):
        from fastapi.testclient import TestClient
        import api

        client = TestClient(api.app)
        response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "solo-studio-video")

    def test_video_prefixed_api_routes_work_for_direct_container_smoke(self):
        from fastapi.testclient import TestClient
        import api

        client = TestClient(api.app)

        response = client.get("/video/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "solo-studio-video")

    def test_token_gates_job_routes_but_leaves_health_and_templates_public(self):
        from fastapi.testclient import TestClient
        import api

        old_token = api.API_TOKEN
        old_jobs_file = api.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                test_token = "test-" + "secret"
                api.API_TOKEN = test_token
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(api.JOBS_FILE, lambda _jobs: {})
                client = TestClient(api.app)

                self.assertEqual(client.get("/api/health").status_code, 200)
                self.assertEqual(client.get("/api/templates").status_code, 200)
                self.assertEqual(client.get("/api/jobs").status_code, 401)
                self.assertEqual(client.get("/video/api/jobs").status_code, 401)
                self.assertEqual(
                    client.get("/api/jobs", headers={"Authorization": "Bearer wrong"}).status_code,
                    401,
                )
                self.assertEqual(
                    client.get("/api/jobs", headers={"Authorization": f"Bearer {test_token}"}).status_code,
                    200,
                )
                self.assertEqual(
                    client.get("/video/api/jobs", headers={"X-Solo-Studio-Token": test_token}).status_code,
                    200,
                )
                self.assertEqual(
                    client.post(
                        "/api/jobs",
                        headers={"Authorization": f"Bearer {test_token}", "Origin": "https://evil.example"},
                        json={"topic": "cross-origin mutation"},
                    ).status_code,
                    403,
                )
                self.assertEqual(client.get("/api/jobs/missing").status_code, 401)
                self.assertEqual(
                    client.get("/api/jobs/missing", headers={"Authorization": f"Bearer {test_token}"}).status_code,
                    404,
                )
            finally:
                api.API_TOKEN = old_token
                api.JOBS_FILE = old_jobs_file

    def test_create_job_rejects_boolean_or_non_finite_duration_minutes(self):
        from fastapi.testclient import TestClient
        import api

        old_token = api.API_TOKEN
        old_jobs_file = api.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                test_token = "test-" + "secret"
                api.API_TOKEN = test_token
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(api.JOBS_FILE, lambda _jobs: {})
                client = TestClient(api.app)
                headers = {"Authorization": f"Bearer {test_token}"}

                bool_response = client.post(
                    "/api/jobs",
                    headers=headers,
                    json={"topic": "bad", "duration_minutes": True},
                )
                self.assertIn(bool_response.status_code, [400, 422])

                nan_payload = "{\"topic\": \"bad\", \"duration_minutes\": NaN}"
                nan_response = client.post(
                    "/api/jobs",
                    headers={**headers, "Content-Type": "application/json"},
                    content=nan_payload,
                )
                self.assertNotEqual(nan_response.status_code, 500)
                self.assertIn(nan_response.status_code, [400, 422])
            finally:
                api.API_TOKEN = old_token
                api.JOBS_FILE = old_jobs_file

    def test_template_job_rejects_invalid_duration_with_unprocessable_entity(self):
        from fastapi.testclient import TestClient
        import api

        old_token = api.API_TOKEN
        old_jobs_file = api.JOBS_FILE
        old_api_file = api.__file__
        old_attempts = {key: list(values) for key, values in api.SESSION_LOGIN_ATTEMPTS.items()}
        with tempfile.TemporaryDirectory() as tmp:
            try:
                test_token = "test-" + "secret"
                api.API_TOKEN = test_token
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(api.JOBS_FILE, lambda _jobs: {})
                (Path(tmp) / "templates.json").write_text(json.dumps([
                    {
                        "id": "bad-duration",
                        "topic": "bad",
                        "target_audience": "general",
                        "duration_minutes": True,
                        "platform": "youtube",
                        "tone": "professional",
                    },
                    {
                        "id": "oversized-duration",
                        "topic": "bad",
                        "target_audience": "general",
                        "duration_minutes": 10**4000,
                        "platform": "youtube",
                        "tone": "professional",
                    }
                ]))
                api.__file__ = str(Path(tmp) / "api.py")
                client = TestClient(api.app)

                bool_response = client.post(
                    "/api/jobs/from-template/bad-duration",
                    headers={"Authorization": f"Bearer {test_token}"},
                )

                self.assertEqual(bool_response.status_code, 422)
                self.assertEqual(bool_response.json()["detail"], "duration_minutes must be a real number")

                oversized_response = client.post(
                    "/api/jobs/from-template/oversized-duration",
                    headers={"Authorization": f"Bearer {test_token}"},
                )

                self.assertEqual(oversized_response.status_code, 422)
                self.assertEqual(oversized_response.json()["detail"], "duration_minutes must be a number")
            finally:
                api.API_TOKEN = old_token
                api.JOBS_FILE = old_jobs_file
                api.__file__ = old_api_file
                api.SESSION_LOGIN_ATTEMPTS.clear()
                api.SESSION_LOGIN_ATTEMPTS.update(old_attempts)

    def test_api_operator_token_login_sets_httponly_cookie_and_job_routes_accept_cookie(self):
        from fastapi.testclient import TestClient
        import api

        old_token = api.API_TOKEN
        old_jobs_file = api.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                test_token = "test-" + "secret"
                api.API_TOKEN = test_token
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(api.JOBS_FILE, lambda _jobs: {})
                client = TestClient(api.app)

                same_origin = {"Origin": "https://edgescout.tech"}
                response = client.post("/api/auth/session", headers=same_origin, json={"token": test_token})
                self.assertEqual(response.status_code, 204)
                cookie = response.headers.get("set-cookie", "")
                self.assertIn("solo_studio_session=", cookie)
                self.assertIn("HttpOnly", cookie)
                self.assertIn("SameSite", cookie)
                self.assertNotIn(test_token, cookie)

                self.assertEqual(client.get("/api/jobs").status_code, 200)

                logout = client.post("/api/auth/logout", headers=same_origin)
                self.assertEqual(logout.status_code, 204)
                self.assertEqual(client.get("/api/jobs").status_code, 401)
            finally:
                api.API_TOKEN = old_token
                api.JOBS_FILE = old_jobs_file

    def test_api_session_login_rate_limit_and_expired_session_fail_closed(self):
        from fastapi.testclient import TestClient
        import api
        import time

        old_token = api.API_TOKEN
        old_jobs_file = api.JOBS_FILE
        old_attempts = api.SESSION_LOGIN_ATTEMPTS.copy()
        old_max_attempts = api.SESSION_LOGIN_MAX_ATTEMPTS
        old_cookie_secure = api.COOKIE_SECURE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                api.API_TOKEN = "test-" + "secret"
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(api.JOBS_FILE, lambda _jobs: {})
                api.SESSION_LOGIN_ATTEMPTS.clear()
                api.SESSION_LOGIN_MAX_ATTEMPTS = 2
                api.COOKIE_SECURE = True
                client = TestClient(api.app)

                same_origin = {"Origin": "https://edgescout.tech"}
                self.assertEqual(client.post("/api/auth/session", headers=same_origin, json={"token": "wrong"}).status_code, 401)
                self.assertEqual(client.post("/api/auth/session", headers=same_origin, json={"token": "wrong"}).status_code, 401)
                limited = client.post("/api/auth/session", headers=same_origin, json={"token": "wrong"})
                self.assertEqual(limited.status_code, 429)
                self.assertIn("Retry-After", limited.headers)

                client.cookies.set(api.SESSION_COOKIE_NAME, "expired-session")
                api.SESSION_TOKENS["expired-session"] = time.time() - 1
                api.SESSION_LOGIN_ATTEMPTS.clear()
                self.assertEqual(client.get("/api/jobs").status_code, 401)
            finally:
                api.API_TOKEN = old_token
                api.JOBS_FILE = old_jobs_file
                api.SESSION_LOGIN_ATTEMPTS.clear()
                api.SESSION_LOGIN_ATTEMPTS.update(old_attempts)
                api.SESSION_LOGIN_MAX_ATTEMPTS = old_max_attempts
                api.COOKIE_SECURE = old_cookie_secure

    def test_api_cors_is_not_wildcard_by_default(self):
        source = (ROOT / "api.py").read_text()

        self.assertIn("SOLO_STUDIO_CORS_ORIGINS", source)
        self.assertIn("SOLO_STUDIO_REQUIRE_API_TOKEN", source)
        self.assertIn("RuntimeError", source)
        self.assertIn("allow_origins=ALLOWED_ORIGINS", source)
        self.assertIn("REQUIRE_API_TOKEN or API_TOKEN", source)
        self.assertIn("SESSION_LOGIN_MAX_ATTEMPTS", source)
        self.assertNotIn("allow_origins=[\"*\"]", source)

    def test_save_jobs_is_atomic_and_create_flow_does_not_overwrite_worker_updates(self):
        import api

        old_jobs_file = api.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(
                    api.JOBS_FILE,
                    lambda _jobs: {"old": {"id": "old", "status": "running", "created_at": "t1"}},
                )

                api._add_job_locked("new", {"id": "new", "status": "queued", "created_at": "t2"})

                saved = json.loads(api.JOBS_FILE.read_text())
                self.assertEqual(saved["old"]["status"], "running")
                self.assertEqual(saved["new"]["status"], "queued")
            finally:
                api.JOBS_FILE = old_jobs_file

    def test_api_corrupt_jobs_store_returns_503_instead_of_empty_state(self):
        from fastapi.testclient import TestClient
        import api

        old_jobs_file = api.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            api.JOBS_FILE = Path(tmp) / "jobs.json"
            api.JOBS_FILE.write_text('{"broken":')
            try:
                client = TestClient(api.app)
                self.assertEqual(client.get("/api/jobs").status_code, 503)
                self.assertEqual(client.get("/api/jobs/known-job").status_code, 503)
                response = client.post("/api/jobs", json={
                    "topic": "x",
                    "target_audience": "y",
                    "duration_minutes": 1,
                    "platform": "youtube",
                    "tone": "professional",
                })
                self.assertEqual(response.status_code, 503)
                self.assertEqual(api.JOBS_FILE.read_text(), '{"broken":')
            finally:
                api.JOBS_FILE = old_jobs_file

    def test_api_jobs_store_oserror_returns_503_instead_of_raw_500(self):
        from fastapi.testclient import TestClient
        import api

        client = TestClient(api.app)
        with patch("api.read_json_object", side_effect=OSError("lock file permission denied")):
            self.assertEqual(client.get("/api/jobs").status_code, 503)
            self.assertEqual(client.get("/api/jobs/known-job").status_code, 503)

        with patch("api.update_json_file", side_effect=OSError("lock file permission denied")):
            response = client.post("/api/jobs", json={
                "topic": "x",
                "target_audience": "y",
                "duration_minutes": 1,
                "platform": "youtube",
                "tone": "professional",
            })
            self.assertEqual(response.status_code, 503)

    def test_api_artifact_enrichment_uses_threadpool_from_async_routes(self):
        source = (ROOT / "api.py").read_text()
        self.assertIn("from starlette.concurrency import run_in_threadpool", source)
        self.assertIn("return await run_in_threadpool(_enrich_jobs", source)
        self.assertIn("return await run_in_threadpool(_enrich_job", source)
        self.assertIn("await run_in_threadpool(_write_download_manifest", source)
        self.assertIn("def _enrich_jobs", source)
        self.assertIn("def _write_download_manifest", source)

    def test_job_status_and_download_include_artifact_manifest(self):
        from fastapi.testclient import TestClient
        import api

        old_jobs_file = api.JOBS_FILE
        old_output_root = api.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_id = "api-test-job"
            output = root / "output" / job_id
            (output / "audio").mkdir(parents=True)
            fixtures = {
                "creative_brief.json": "{}",
                "script.txt": "script",
                "storyboard.json": json.dumps({"scenes": [{"scene_number": 1}]}),
                "video_prompts.json": json.dumps({"scenes": []}),
                "audio/voiceover_script.txt": "voiceover",
                "music_prompt.txt": "music",
                "captions.srt": "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
                "assembly_manifest.json": "{}",
                "timeline.fcpxml": "<fcpxml></fcpxml>",
            }
            for rel, content in fixtures.items():
                path = output / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            api.JOBS_FILE = root / "jobs.json"
            api.OUTPUT_ROOT = root / "output"
            update_json_file(api.JOBS_FILE, lambda _jobs: {
                job_id: {
                    "id": job_id,
                    "topic": "API package test",
                    "status": "completed",
                    "duration_seconds": 60,
                    "created_at": "2026-08-17T00:00:00+00:00",
                }
            })

            try:
                client = TestClient(api.app)
                status_response = client.get(f"/api/jobs/{job_id}")
                self.assertEqual(status_response.status_code, 200)
                self.assertEqual(status_response.json()["package_status"], "editor_package")

                download_response = client.get(f"/api/jobs/{job_id}/download")
                self.assertEqual(download_response.status_code, 200)
                package = zipfile.ZipFile(io.BytesIO(download_response.content))
                self.assertIn("package_manifest.json", package.namelist())
                manifest = json.loads(package.read("package_manifest.json"))
                self.assertEqual(manifest["package_status"], "editor_package")
            finally:
                api.JOBS_FILE = old_jobs_file
                api.OUTPUT_ROOT = old_output_root

    def test_write_brief_yaml_uses_safe_yaml_for_quotes_and_newlines(self):
        import yaml
        import api

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brief.yaml"
            job = {
                "topic": "Quoted \"topic\"\nwith newline",
                "target_audience": "founders: CTOs",
                "duration_seconds": 90,
                "platform": "youtube",
                "tone": "educational",
                "key_messages": ["Ship faster: review harder", "Line\nbreak"],
                "visual_style": "dark: cinematic",
                "call_to_action": "Subscribe \"now\"",
            }

            api._write_brief_yaml(path, job)
            parsed = yaml.safe_load(path.read_text())

            self.assertEqual(parsed["topic"], job["topic"])
            self.assertEqual(parsed["key_messages"], job["key_messages"])
            self.assertEqual(parsed["visual_style"], job["visual_style"])

    def test_dockerfile_fails_container_when_critical_process_exits(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        dockerignore = (ROOT / ".dockerignore").read_text()

        self.assertIn("wait -n", dockerfile)
        self.assertIn("ENV SOLO_STUDIO_REQUIRE_API_TOKEN=1", dockerfile)
        self.assertIn("nginx_pid=$!", dockerfile)
        self.assertIn("api_pid=$!", dockerfile)
        self.assertIn("worker_pid=$!", dockerfile)
        self.assertIn("if [ -n \"$worker_pid\" ]; then", dockerfile)
        self.assertIn("kill \"$nginx_pid\" \"$api_pid\" \"$worker_pid\"", dockerfile)
        self.assertIn("kill \"$nginx_pid\" \"$api_pid\"", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("/api/health", dockerfile)
        for sensitive_path in (".git/", ".solo_studio_api_token", "jobs.json", "output/", "tests/"):
            self.assertIn(sensitive_path, dockerignore)

    def test_dockerfile_normalizes_forwarded_client_ip_to_source_proxy(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertIn("proxy_set_header X-Real-IP \\$remote_addr;", dockerfile)
        self.assertIn("proxy_set_header X-Forwarded-For \\$remote_addr;", dockerfile)
        self.assertNotIn("proxy_set_header X-Forwarded-For $http_x_forwarded_for", dockerfile)

    def test_deploy_script_requires_token_and_smokes_protected_jobs_route(self):
        deploy = (ROOT / "deploy-traefik.sh").read_text()

        self.assertIn("SOLO_STUDIO_API_TOKEN:?", deploy)
        self.assertIn("-e SOLO_STUDIO_API_TOKEN", deploy)
        self.assertIn("-e SOLO_STUDIO_REQUIRE_API_TOKEN=1", deploy)
        self.assertIn("-e SOLO_STUDIO_TRUST_PROXY_HEADERS=1", deploy)
        self.assertIn("-e SOLO_STUDIO_TRUSTED_PROXY_NETWORKS=127.0.0.1/32,::1/128", deploy)
        self.assertIn("-e SOLO_STUDIO_SESSION_COOKIE_PATH=/video", deploy)
        self.assertIn("SOLO_STUDIO_CORS_ORIGINS", deploy)
        self.assertIn('case "${SOLO_STUDIO_ENABLE_HIGGSFIELD,,}"', deploy)
        self.assertIn("curl_config=$(mktemp)", deploy)
        self.assertIn("chmod 600 \"$curl_config\"", deploy)
        self.assertIn("printf '%s\\n'", deploy)
        self.assertIn("Authorization: Bearer", deploy)
        self.assertIn('> \"$curl_config\"', deploy)
        self.assertNotIn("Authorization: Bearer ***", deploy)
        self.assertIn("SOLO_STUDIO_CURL_CONNECT_TIMEOUT", deploy)
        self.assertIn("SOLO_STUDIO_CURL_MAX_TIME", deploy)
        self.assertIn("CURL_BOUNDED=(--connect-timeout", deploy)
        self.assertIn('curl "${CURL_BOUNDED[@]}" -fsS --config "$curl_config"', deploy)
        self.assertNotIn('curl -fsS -H "Authorization: Bearer ***', deploy)
        self.assertIn("https://edgescout.tech/video/api/jobs?limit=1", deploy)
        self.assertIn("SOLO_STUDIO_DISABLE_WORKER=1", deploy)

    def test_deploy_script_tags_named_rollback_image_before_live_replacement(self):
        deploy = (ROOT / "deploy-traefik.sh").read_text()

        self.assertIn("rollback_tag=\"solo-studio-video:rollback-", deploy)
        self.assertIn("docker tag \"$current_image\" \"$rollback_tag\"", deploy)
        self.assertIn("rollback_live", deploy)
        self.assertIn("run_container_preflight \"$rollback_tag\"", deploy)
        self.assertIn('run_container "$release_tag"', deploy)
        self.assertIn("container_replaced=0", deploy)
        self.assertIn("container_replaced=1", deploy)
        self.assertIn("Container was not replaced yet; leaving existing service untouched.", deploy)
        self.assertIn("LOCAL_HEALTH_OK attempt=$attempt", deploy)
        self.assertIn("Local container smoke failed after $attempt attempts", deploy)
        self.assertIn("wait_for_local_health", deploy)
        self.assertIn("wait_for_public_health", deploy)
        self.assertIn("return 1", deploy)
        self.assertIn("docker run -d", deploy)
        self.assertIn("$rollback_tag", deploy)
        self.assertNotIn("docker rm -f solo-studio-video 2>/dev/null || true\n\n# Ensure jobs.json", deploy)
        self.assertIn("for attempt in", deploy)
        self.assertIn("PUBLIC_HEALTH_OK", deploy)

    def test_provider_cli_is_pinned_and_real_mode_is_explicitly_configured(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        deploy = (ROOT / "deploy-traefik.sh").read_text()

        self.assertIn("@higgsfield/cli@${HIGGSFIELD_CLI_VERSION}", dockerfile)
        self.assertIn("ARG HIGGSFIELD_CLI_VERSION=1.1.23", dockerfile)
        self.assertIn("SOLO_STUDIO_ENABLE_HIGGSFIELD", deploy)
        self.assertIn("SOLO_STUDIO_HIGGSFIELD_MODEL", deploy)
        self.assertIn("HIGGSFIELD_CREDENTIALS_FILE", deploy)
        self.assertIn("credentials.json:ro", deploy)


if __name__ == "__main__":
    unittest.main()
