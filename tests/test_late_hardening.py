import hashlib
import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import api
import audio_generation
import job_store
import worker
import package_utils
from package_utils import update_json_file


class ImportConfigurationTests(unittest.TestCase):
    def test_api_token_fifo_does_not_block_module_import(self):
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix="hermes-verify-") as tmp:
            token_fifo = Path(tmp) / "token.fifo"
            os.mkfifo(token_fifo)
            environment = os.environ.copy()
            environment["SOLO_STUDIO_API_TOKEN_FILE"] = str(token_fifo)
            environment["SOLO_STUDIO_REQUIRE_API_TOKEN"] = "0"
            environment.pop("SOLO_STUDIO_API_TOKEN", None)
            result = subprocess.run(
                [sys.executable, "-c", "import api; assert api.API_TOKEN == ''"],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_api_token_file_rejects_surrounding_whitespace(self):
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix="hermes-verify-") as tmp:
            root = Path(tmp)
            environment = os.environ.copy()
            environment["SOLO_STUDIO_REQUIRE_API_TOKEN"] = "0"
            environment.pop("SOLO_STUDIO_API_TOKEN", None)
            for content, expected in (("synthetic-token", "synthetic-token"), ("synthetic-token\n", "synthetic-token"), (" synthetic-token", ""), ("synthetic-token ", ""), ("synthetic token", "")):
                token_file = root / "token"
                token_file.write_text(content, encoding="utf-8")
                token_file.chmod(0o600)
                environment["SOLO_STUDIO_API_TOKEN_FILE"] = str(token_file)
                result = subprocess.run(
                    [sys.executable, "-c", "import api; print(api.API_TOKEN, end='')"],
                    cwd=Path(__file__).resolve().parents[1],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_external_database_root_parity_holds_at_module_import(self):
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix="hermes-verify-") as tmp:
            database = Path(tmp) / "external" / "state.sqlite3"
            environment = os.environ.copy()
            environment["SOLO_STUDIO_DATABASE_FILE"] = str(database)
            environment["SOLO_STUDIO_REQUIRE_API_TOKEN"] = "0"
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import api, worker; print(api.OUTPUT_ROOT); print(worker.OUTPUT_ROOT)",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            roots = result.stdout.splitlines()
            self.assertEqual(len(roots), 2, result.stdout)
            self.assertEqual(roots[0], roots[1])
    def test_bounded_subprocess_reaps_detached_grandchild(self):
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix="hermes-verify-") as tmp:
            marker = Path(tmp) / "detached-pid"
            child_code = (
                "import os,time\n"
                "pid=os.fork()\n"
                "if pid:\n"
                "    os._exit(0)\n"
                "os.setsid()\n"
                f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
                "time.sleep(30)\n"
            )
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                package_utils._run_bounded_subprocess(
                    [sys.executable, "-c", child_code], timeout=0.1
                )
            self.assertLess(time.monotonic() - started, 1.0)
            for _ in range(20):
                if marker.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            detached_pid = int(marker.read_text())
            try:
                state = Path(f"/proc/{detached_pid}/stat").read_text()
            except FileNotFoundError:
                state = ""
            self.assertFalse(state.rsplit(")", 1)[-1].lstrip().startswith("Z "))


class LegacyIdempotencyTests(unittest.TestCase):
    def test_legacy_api_read_rejects_malformed_lifecycle_record(self):
        old_database_configured = api.DATABASE_CONFIGURED
        old_jobs_file = api.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                api.DATABASE_CONFIGURED = False
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(api.JOBS_FILE, lambda _jobs: {
                    "legacy-invalid": {"id": "legacy-invalid", "status": "bogus"},
                })
                with self.assertRaises(api.HTTPException) as raised:
                    api._load_jobs_for_api()
                self.assertEqual(raised.exception.status_code, 503)
            finally:
                api.DATABASE_CONFIGURED = old_database_configured
                api.JOBS_FILE = old_jobs_file

    def test_legacy_worker_read_rejects_malformed_lifecycle_record(self):
        old_database_configured = worker.DATABASE_CONFIGURED
        old_jobs_file = worker.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                worker.DATABASE_CONFIGURED = False
                worker.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(worker.JOBS_FILE, lambda _jobs: {
                    "legacy-invalid": {"id": "legacy-invalid", "progress": "0.5"},
                })
                with self.assertRaises(job_store.InvalidStoreState):
                    worker.load_jobs()
            finally:
                worker.DATABASE_CONFIGURED = old_database_configured
                worker.JOBS_FILE = old_jobs_file

    def test_legacy_read_rejects_non_string_topic(self):
        with self.assertRaises(job_store.InvalidStoreState):
            job_store._validate_legacy_record(
                "legacy-invalid-topic", {"id": "legacy-invalid-topic", "topic": {}},
            )

    def test_explicit_empty_stage_configuration_is_rejected(self):
        with self.assertRaises(job_store.InvalidStoreState):
            job_store._validate_stage_names([])

    def test_legacy_terminal_timestamps_cannot_contradict_status(self):
        with self.assertRaises(job_store.InvalidStoreState):
            job_store._validate_legacy_record(
                "legacy-completed-cancelled",
                {"id": "legacy-completed-cancelled", "status": "completed", "cancelled_at": "2026-08-26T10:00:00+00:00"},
            )
        with self.assertRaises(job_store.InvalidStoreState):
            job_store._validate_legacy_record(
                "legacy-cancelled-completed",
                {"id": "legacy-cancelled-completed", "status": "cancelled", "completed_at": "2026-08-26T10:00:00+00:00"},
            )

    def test_legacy_json_backend_returns_original_job_for_same_owner_key(self):
        old_database_configured = api.DATABASE_CONFIGURED
        old_jobs_file = api.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                api.DATABASE_CONFIGURED = False
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                first_payload = {
                    "id": "legacy-one",
                    "topic": "same request",
                    "owner_id": "owner-a",
                    "idempotency_key": "request-1",
                }
                second_payload = {
                    "id": "legacy-two",
                    "topic": "duplicate request",
                    "owner_id": "owner-a",
                }
                first, first_created = api._create_job_locked("legacy-one", first_payload, "request-1")
                second, second_created = api._create_job_locked("legacy-two", second_payload, " request-1 ")
                self.assertTrue(first_created)
                self.assertFalse(second_created)
                self.assertEqual(second["id"], first["id"])
                self.assertEqual(len(api.read_json_object(api.JOBS_FILE)), 1)
            finally:
                api.DATABASE_CONFIGURED = old_database_configured
                api.JOBS_FILE = old_jobs_file

    def test_legacy_lock_symlink_is_rejected_without_truncating_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs.json"
            target = root / "outside.txt"
            target.write_text("must survive")
            jobs.write_text("{}")
            jobs.with_name("jobs.json.lock").symlink_to(target)
            with self.assertRaises((OSError, ValueError)):
                update_json_file(jobs, lambda payload: {**payload, "new": {"id": "new"}})
            self.assertEqual(target.read_text(), "must survive")

    def test_api_enrichment_rejects_path_traversal_job_id(self):
        with self.assertRaises(job_store.InvalidStoreState):
            api._enrich_job({"id": "../outside", "status": "completed"})


class TtsTemporaryArtifactTests(unittest.TestCase):
    def test_tts_rejects_non_https_endpoint_before_network_call(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "SOLO_STUDIO_ENABLE_TTS": "1",
                "ELEVENLABS_API_KEY": "test-only-key",
                "SOLO_STUDIO_TTS_ENDPOINT": "http://attacker.invalid/tts",
            },
            clear=False,
        ), patch("audio_generation._open_tts_request") as urlopen:
            with self.assertRaises(audio_generation.AudioGenerationError):
                audio_generation.generate_voiceover("hello", Path(tmp) / "voiceover.mp3")
            urlopen.assert_not_called()

    def test_preexisting_temporary_symlink_is_not_followed(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"audio-bytes"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "voiceover.mp3"
            outside = root / "outside.bin"
            outside.write_bytes(b"original")
            with patch("audio_generation.secrets.token_hex", return_value="1"):
                temporary = destination.with_name(f".{destination.name}.part-{os.getpid()}-1")
            temporary.symlink_to(outside)
            with patch.dict(
                os.environ,
                {
                    "SOLO_STUDIO_ENABLE_TTS": "1",
                    "ELEVENLABS_API_KEY": "test-only-key",
                    "SOLO_STUDIO_TTS_ATTEMPTS": "1",
                },
                clear=False,
            ), patch("audio_generation.secrets.token_hex", return_value="1"), patch(
                "audio_generation._open_tts_request", return_value=Response()), patch(
                "audio_generation.probe_media", return_value={"has_audio": True, "duration_seconds": 1.0}
            ):
                with self.assertRaises(audio_generation.AudioGenerationError):
                    audio_generation.generate_voiceover("hello", destination)
            self.assertEqual(outside.read_bytes(), b"original")
            self.assertFalse(destination.exists())


class GeneratedArtifactCleanupRaceTests(unittest.TestCase):
    def test_clear_generated_artifacts_preserves_replacement_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "visuals"
            original.mkdir()
            (original / "original.txt").write_text("original", encoding="utf-8")
            replacement = root / "visuals"
            real_remove_tree = package_utils._remove_tree_at

            def swap_before_remove(parent_fd, name, expected_inode=None):
                if name == "visuals":
                    os.rename(root / "visuals", root / "visuals-original")
                    replacement.mkdir()
                    (replacement / "replacement.txt").write_text("replacement", encoding="utf-8")
                return real_remove_tree(parent_fd, name, expected_inode)

            with patch("package_utils._remove_tree_at", side_effect=swap_before_remove):
                with self.assertRaises(ValueError):
                    package_utils.clear_generated_artifacts(root)
            self.assertEqual((root / "visuals" / "replacement.txt").read_text(encoding="utf-8"), "replacement")
            self.assertEqual((root / "visuals-original" / "original.txt").read_text(encoding="utf-8"), "original")


class CancellationRaceTests(unittest.TestCase):
    def test_legacy_worker_update_cannot_overwrite_cancelled_job(self):
        old_database_configured = worker.DATABASE_CONFIGURED
        old_jobs_file = worker.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                worker.DATABASE_CONFIGURED = False
                worker.JOBS_FILE = Path(tmp) / "jobs.json"
                worker.update_json_file(
                    worker.JOBS_FILE,
                    lambda _jobs: {
                        "cancelled-job": {"id": "cancelled-job", "status": "cancelled"},
                    },
                )
                with self.assertRaises(job_store.LeaseLost):
                    worker.update_job("cancelled-job", status="completed")
                self.assertEqual(worker.read_json_object(worker.JOBS_FILE)["cancelled-job"]["status"], "cancelled")
            finally:
                worker.DATABASE_CONFIGURED = old_database_configured
                worker.JOBS_FILE = old_jobs_file


class ResumeAndIdentifierTests(unittest.TestCase):
    def test_legacy_json_and_cleanup_reject_symlink_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs.json"
            outside = root / "outside.json"
            outside.write_text("{}")
            jobs.symlink_to(outside)
            with self.assertRaises(ValueError):
                worker.update_json_file(jobs, lambda payload: payload)
            with self.assertRaises(job_store.InvalidStoreState):
                job_store.import_jobs_json_once(jobs, path=root / "jobs.sqlite3")

            output = root / "output"
            outside_dir = root / "outside-dir"
            outside_dir.mkdir()
            (outside_dir / "keep.txt").write_text("keep")
            output.symlink_to(outside_dir, target_is_directory=True)
            with self.assertRaises(ValueError):
                worker.clear_generated_artifacts(output)
            self.assertTrue((outside_dir / "keep.txt").exists())

    def test_resume_and_assembly_reject_malformed_or_duplicate_scene_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storyboard = root / "storyboard.json"
            storyboard.write_text(json.dumps({"scenes": [42]}))
            self.assertFalse(worker._resume_artifacts_valid(root, "script", ("storyboard.json",)))
            storyboard.write_text(json.dumps({"scenes": [{"scene_number": 1}, {"scene_number": 1}]}))
            self.assertFalse(worker._resume_artifacts_valid(root, "script", ("storyboard.json",)))

            clips = root / "clips"
            clips.mkdir()
            (clips / "generation_plan.json").write_text(
                json.dumps({"status": "completed", "total_scenes": 2, "scenes": [{"scene_number": 1}, {"scene_number": 1}]})
            )
            with self.assertRaises(worker.MediaError):
                worker._assemble_verified_output(
                    root,
                    {"scenes": [{"scene_number": 1}, {"scene_number": 1}], "total_duration": 2},
                )
    def test_job_store_rejects_control_characters_before_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "jobs.sqlite3"
            with self.assertRaises(job_store.InvalidStoreState):
                job_store.create_job("bad\njob", {}, path=database)
            with self.assertRaises(job_store.InvalidStoreState):
                job_store.create_job("valid-job", {}, owner_id="bad\towner", path=database)
            self.assertFalse(database.exists())

    def test_resume_validation_rejects_malformed_json_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storyboard = root / "storyboard.json"
            storyboard.write_text(json.dumps({"scenes": [{"scene_number": 1}]}))
            self.assertTrue(worker._resume_artifacts_valid(root, "script", ("storyboard.json",)))

            storyboard.write_text("not-json")
            self.assertFalse(worker._resume_artifacts_valid(root, "script", ("storyboard.json",)))

            outside = root / "outside.json"
            outside.write_text(json.dumps({"scenes": [{"scene_number": 1}]}))
            storyboard.unlink()
            storyboard.symlink_to(outside)
            self.assertFalse(worker._resume_artifacts_valid(root, "script", ("storyboard.json",)))

            storyboard.unlink()
            storyboard.symlink_to(root / "missing.json")
            self.assertFalse(worker._resume_artifacts_valid(root, "script", ("storyboard.json",)))

    def test_resume_validation_rejects_failed_generation_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            plan = clips / "generation_plan.json"
            plan.write_text(json.dumps({"status": "failed", "total_scenes": 1, "scenes": [{}]}))
            self.assertFalse(worker._resume_artifacts_valid(root, "video_generation", ("clips/generation_plan.json",)))

    def test_assembly_rejects_storyboard_generation_plan_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            (root / "storyboard.json").write_text(json.dumps({"scenes": [{"scene_number": 1}, {"scene_number": 2}]}))
            (clips / "generation_plan.json").write_text(
                json.dumps({"status": "completed", "total_scenes": 2, "scenes": [{"scene_number": 1}, {"scene_number": 3}]})
            )
            with self.assertRaises(worker.MediaError):
                worker._assemble_verified_output(
                    root,
                    {"scenes": [{"scene_number": 1}, {"scene_number": 2}], "total_duration": 2},
                )
    def test_resume_validation_accepts_dry_run_generation_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            (clips / "generation_plan.json").write_text(json.dumps({
                "status": "dry_run",
                "total_scenes": 1,
                "scenes": [{
                    "scene_number": 1,
                    "target_file": "clips/scene_01.mp4",
                    "status": "dry_run",
                }],
            }))
            self.assertTrue(worker._resume_artifacts_valid(root, "video_generation", ("clips/generation_plan.json",)))

    def test_resume_validation_rejects_incoherent_generation_plan_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            plan = clips / "generation_plan.json"
            base_scene = {"scene_number": 1, "target_file": "clips/scene_01.mp4"}
            plan.write_text(json.dumps({
                "status": "completed",
                "total_scenes": 1,
                "scenes": [{**base_scene, "status": "dry_run"}],
            }))
            self.assertFalse(worker._resume_artifacts_valid(root, "video_generation", ("clips/generation_plan.json",)))
            plan.write_text(json.dumps({
                "status": "dry_run",
                "total_scenes": 1,
                "scenes": [{**base_scene, "status": "downloaded"}],
            }))
            self.assertFalse(worker._resume_artifacts_valid(root, "video_generation", ("clips/generation_plan.json",)))

    def test_resume_validation_rejects_completed_plan_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            clip = clips / "scene_01.mp4"
            clip.write_bytes(b"synthetic-clip")
            digest = hashlib.sha256(clip.read_bytes()).hexdigest()
            plan = clips / "generation_plan.json"
            plan.write_text(json.dumps({
                "status": "completed",
                "total_scenes": 1,
                "scenes": [{
                    "scene_number": 1,
                    "target_file": "clips/scene_01.mp4",
                    "status": "verified",
                    "sha256": digest,
                    "duration_seconds": 1.0,
                }],
            }))
            self.assertTrue(worker._resume_artifacts_valid(root, "video_generation", ("clips/generation_plan.json",)))
            plan.write_text(json.dumps({
                "status": "completed",
                "total_scenes": 1,
                "scenes": [{
                    "scene_number": 1,
                    "target_file": "clips/scene_01.mp4",
                    "status": "verified",
                    "sha256": "0" * 64,
                    "duration_seconds": 1.0,
                }],
            }))
            self.assertFalse(worker._resume_artifacts_valid(root, "video_generation", ("clips/generation_plan.json",)))

    def test_provenance_backed_dry_run_resume_accepts_plan_without_mp4(self):
        old_database_configured = worker.DATABASE_CONFIGURED
        with tempfile.TemporaryDirectory() as tmp:
            try:
                worker.DATABASE_CONFIGURED = False
                root = Path(tmp)
                clips = root / "clips"
                clips.mkdir()
                brief = root / "brief.yaml"
                brief.write_text("topic: dry run\n")
                storyboard = {"scenes": [{"scene_number": 1}]}
                prompts = {"scenes": [{"scene_number": 1}]}
                plan = {
                    "status": "dry_run",
                    "total_scenes": 1,
                    "scenes": [{
                        "scene_number": 1,
                        "target_file": "clips/scene_01.mp4",
                        "status": "dry_run",
                    }],
                }
                (root / "storyboard.json").write_text(json.dumps(storyboard))
                (root / "video_prompts.json").write_text(json.dumps(prompts))
                (clips / "generation_plan.json").write_text(json.dumps(plan))
                worker._record_stage_provenance(
                    root, "dry-run-job", "video_generation", ("clips/generation_plan.json",)
                )
                self.assertTrue(
                    worker._resume_artifacts_valid(
                        root, "video_generation", ("clips/generation_plan.json",), "dry-run-job"
                    )
                )
            finally:
                worker.DATABASE_CONFIGURED = old_database_configured

    def test_legacy_worker_rejects_terminal_job_mutation(self):
        old_database_configured = worker.DATABASE_CONFIGURED
        old_jobs_file = worker.JOBS_FILE
        old_output_root = worker.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            try:
                worker.DATABASE_CONFIGURED = False
                worker.JOBS_FILE = Path(tmp) / "jobs.json"
                worker.OUTPUT_ROOT = Path(tmp) / "output"
                completed = {
                    "id": "legacy-cancelled",
                    "topic": "done",
                    "status": "cancelled",
                    "stage": "assembly",
                    "progress": 1.0,
                    "cancelled_at": "2026-01-01T00:00:00+00:00",
                    "output_dir": str(worker.OUTPUT_ROOT / "legacy-cancelled"),
                }
                update_json_file(worker.JOBS_FILE, lambda _jobs: {"legacy-cancelled": completed})
                with self.assertRaises(job_store.LeaseLost):
                    worker.update_job("legacy-cancelled", status="running", stage="research", progress=0.1)
                self.assertEqual(worker.load_jobs()["legacy-cancelled"]["status"], "cancelled")
            finally:
                worker.DATABASE_CONFIGURED = old_database_configured
                worker.JOBS_FILE = old_jobs_file
                worker.OUTPUT_ROOT = old_output_root

    def test_sqlite_preparation_failure_writes_manifest_under_current_attempt(self):
        old = {
            name: getattr(worker, name)
            for name in ("DATABASE_CONFIGURED", "DATABASE_FILE", "OUTPUT_ROOT", "CURRENT_WORKER_ID", "CURRENT_RUN_ID", "WORKER_ID")
        }
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as tmp, patch.dict(
            os.environ, {"SOLO_STUDIO_RETRY_STAGE_FAILURES": "0"}, clear=False
        ):
            try:
                root = Path(tmp)
                worker.DATABASE_CONFIGURED = True
                worker.DATABASE_FILE = root / "state" / "jobs.sqlite"
                worker.OUTPUT_ROOT = root / "output"
                worker.WORKER_ID = "preparation-failure-worker"
                worker.CURRENT_WORKER_ID = worker.WORKER_ID
                worker.OUTPUT_ROOT.mkdir(parents=True)
                job_store.create_job(
                    "preparation-failure-job",
                    {"id": "preparation-failure-job", "topic": "failure"},
                    output_dir=worker.OUTPUT_ROOT / "preparation-failure-job",
                    max_retries=0,
                    path=worker.DATABASE_FILE,
                )
                claim = job_store.claim_next_job(worker.WORKER_ID, path=worker.DATABASE_FILE)
                self.assertIsNotNone(claim)
                assert claim is not None
                worker.CURRENT_RUN_ID = claim.job["run_id"]
                with patch.object(worker, "_prepare_attempt_output", side_effect=RuntimeError("injected preparation failure")):
                    worker.process_job("preparation-failure-job", claim.job)

                failed = job_store.get_job("preparation-failure-job", path=worker.DATABASE_FILE)
                self.assertIsNotNone(failed)
                assert failed is not None
                self.assertEqual(failed["status"], "failed")
                attempt_dir = worker.OUTPUT_ROOT / "preparation-failure-job" / "runs" / claim.job["run_id"]
                self.assertTrue((attempt_dir / "package_manifest.json").is_file())
                self.assertFalse((worker.OUTPUT_ROOT / "preparation-failure-job" / "package_manifest.json").exists())
            finally:
                for name, value in old.items():
                    setattr(worker, name, value)

    def test_sqlite_worker_dry_run_reaches_editor_package_terminal_state(self):
        old = {
            name: getattr(worker, name)
            for name in ("DATABASE_CONFIGURED", "DATABASE_FILE", "OUTPUT_ROOT", "CURRENT_WORKER_ID", "CURRENT_RUN_ID", "WORKER_ID")
        }
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as tmp:
            try:
                root = Path(tmp)
                worker.DATABASE_CONFIGURED = True
                worker.DATABASE_FILE = root / "state" / "jobs.sqlite"
                worker.OUTPUT_ROOT = root / "output"
                worker.WORKER_ID = "dry-run-test-worker"
                worker.CURRENT_WORKER_ID = worker.WORKER_ID
                worker.OUTPUT_ROOT.mkdir(parents=True)
                job_store.create_job(
                    "dry-run-job",
                    {"id": "dry-run-job", "topic": "dry run", "duration_seconds": 60, "platform": "youtube"},
                    output_dir=worker.OUTPUT_ROOT / "dry-run-job",
                    path=worker.DATABASE_FILE,
                )
                claim = job_store.claim_next_job(worker.WORKER_ID, path=worker.DATABASE_FILE)
                self.assertIsNotNone(claim)
                assert claim is not None
                worker.CURRENT_RUN_ID = claim.job["run_id"]
                output = worker.OUTPUT_ROOT / "dry-run-job"
                output.mkdir(parents=True)
                (output / "brief.yaml").write_text("topic: dry run\nduration_minutes: 1\nplatform: youtube\n")
                worker.process_job("dry-run-job", claim.job)
                completed = job_store.get_job("dry-run-job", path=worker.DATABASE_FILE)
                self.assertIsNotNone(completed)
                assert completed is not None
                self.assertEqual(completed["status"], "editor_package")
                self.assertEqual(completed["package_status"], "editor_package")
            finally:
                for name, value in old.items():
                    setattr(worker, name, value)


class WorkerMusicIntegrationTests(unittest.TestCase):
    def test_disabled_music_generation_is_network_free_and_reports_existing_artifact(self):
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix="hermes-verify-") as tmp:
            root = Path(tmp)
            (root / "audio").mkdir()
            (root / "music_prompt.txt").write_text("ambient technology bed", encoding="utf-8")
            with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "0"}, clear=False), patch(
                "worker._probe_media", return_value={"valid": False}
            ) as probe:
                self.assertFalse(worker._generate_music(root, {"total_duration": 60}))
            probe.assert_called_once_with(root / "audio" / "background_music.mp3")

    def test_enabled_music_generation_persists_safe_metadata(self):
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix="hermes-verify-") as tmp:
            root = Path(tmp)
            (root / "music_prompt.txt").write_text("ambient technology bed", encoding="utf-8")
            metadata = {
                "status": "downloaded",
                "provider": "higgsfield",
                "bytes": 123,
                "duration_seconds": 30.0,
                "audio_verified": True,
            }
            def generate_music(prompt, duration, destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"x" * 123)
                identity = os.stat(destination, follow_symlinks=False)
                return {
                    **metadata,
                    "artifact_identity": (identity.st_dev, identity.st_ino),
                    "artifact_sha256": "a" * 64,
                }

            with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}, clear=False), patch(
                "worker.generate_music", side_effect=generate_music
            ) as generate, patch(
                "worker._probe_media",
                return_value={
                    "valid": True,
                    "size_bytes": 123,
                    "duration_seconds": 30.0,
                    "sha256": "a" * 64,
                },
            ):
                self.assertTrue(worker._generate_music(root, {"total_duration": 60}))
            generate.assert_called_once_with(
                "ambient technology bed", 30.0, root / "audio" / "background_music.mp3"
            )
            persisted = json.loads((root / "audio" / "music_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted, {**metadata, "sha256": "a" * 64})

    def test_music_provider_failure_is_normalized(self):
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix="hermes-verify-") as tmp:
            root = Path(tmp)
            (root / "music_prompt.txt").write_text("ambient technology bed", encoding="utf-8")
            with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}, clear=False), patch(
                "worker.generate_music", side_effect=worker.MusicGenerationError("provider failed")
            ):
                self.assertFalse(worker._generate_music(root, {"total_duration": 10}))
            self.assertFalse((root / "audio" / "music_metadata.json").exists())

    def test_invalid_music_metadata_is_rejected_without_persistence(self):
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix="hermes-verify-") as tmp:
            root = Path(tmp)
            (root / "music_prompt.txt").write_text("ambient technology bed", encoding="utf-8")
            invalid_metadata = {
                "status": "downloaded",
                "provider": "higgsfield",
                "bytes": 0,
                "duration_seconds": 10.0,
                "audio_verified": True,
            }
            with patch.dict(os.environ, {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}, clear=False), patch(
                "worker.generate_music", return_value=invalid_metadata
            ):
                self.assertFalse(worker._generate_music(root, {"total_duration": 10}))
            self.assertFalse((root / "audio" / "music_metadata.json").exists())


if __name__ == "__main__":
    unittest.main()
