import json
import io
import threading
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from package_utils import clear_generated_artifacts, compute_package_status, read_json_object, update_json_file, write_package_manifest
from engines.generation_agent import generate_plan


class PackageStatusTests(unittest.TestCase):
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
            clip.write_bytes(b"not a real mp4")

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


class GenerationAgentTests(unittest.TestCase):
    def test_generation_agent_writes_dry_run_plan_without_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
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

        self.assertIn("wait -n", dockerfile)
        self.assertIn("nginx_pid=$!", dockerfile)
        self.assertIn("api_pid=$!", dockerfile)
        self.assertIn("worker_pid=$!", dockerfile)
        self.assertIn("kill \"$nginx_pid\" \"$api_pid\" \"$worker_pid\"", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("/api/health", dockerfile)


if __name__ == "__main__":
    unittest.main()
