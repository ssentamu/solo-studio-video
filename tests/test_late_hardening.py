import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api
import audio_generation
import job_store
import worker
from package_utils import update_json_file


class LegacyIdempotencyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
