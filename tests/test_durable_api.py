import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import job_store
from package_utils import atomic_write_json


class DurableApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.original = {
            "DATABASE_CONFIGURED": api.DATABASE_CONFIGURED,
            "DATABASE_FILE": api.DATABASE_FILE,
            "JOBS_FILE": api.JOBS_FILE,
            "OUTPUT_ROOT": api.OUTPUT_ROOT,
            "API_TOKEN": api.API_TOKEN,
            "REQUIRE_API_TOKEN": api.REQUIRE_API_TOKEN,
        }
        api.DATABASE_CONFIGURED = True
        api.DATABASE_FILE = root / "state" / "solo.sqlite3"
        api.JOBS_FILE = root / "jobs.json"
        api.OUTPUT_ROOT = root / "output"
        api.API_TOKEN = ""
        api.REQUIRE_API_TOKEN = False
        api.OUTPUT_ROOT.mkdir(parents=True)
        job_store.initialize(api.DATABASE_FILE)
        self.client = TestClient(api.app)

    def tearDown(self):
        for key, value in self.original.items():
            setattr(api, key, value)
        self.tmp.cleanup()

    def test_zip_walker_excludes_private_generation_and_backup_directories(self):
        root = Path(self.tmp.name) / "download-root"
        root.mkdir()
        (root / "public.txt").write_text("public", encoding="utf-8")
        for name in (".clips.generation-left", ".clips.previous-left"):
            private = root / name
            private.mkdir()
            (private / "scene_01.mp4").write_bytes(b"private")
        entries = []
        for archive_name, descriptor, _size in api._iter_zip_files_no_follow(root):
            try:
                entries.append(archive_name)
            finally:
                os.close(descriptor)
        self.assertEqual(entries, ["public.txt"])

    def test_zip_snapshot_rejects_file_growth_beyond_captured_size_and_limit(self):
        root = Path(self.tmp.name) / "download-root"
        root.mkdir()
        payload = root / "growth.bin"
        payload.write_bytes(b"too-large")
        descriptor = os.open(payload, os.O_RDONLY)

        def captured_file():
            yield "growth.bin", descriptor, 1

        with patch("api._iter_zip_files_no_follow", return_value=captured_file()), patch.object(
            api, "MAX_DOWNLOAD_BYTES", 1
        ):
            with self.assertRaises(api.HTTPException) as raised:
                api._snapshot_zip_hashes(root, (os.stat(root).st_dev, os.stat(root).st_ino))
        self.assertEqual(raised.exception.status_code, 409)

    def test_template_profile_validation_rejects_malformed_secondary_metadata(self):
        with self.assertRaises(ValueError):
            api.validate_output_profile_contract(
                {"output_profile": "vertical", "aspect_ratio": False},
                "template",
            )

    def test_sqlite_route_is_idempotent_and_does_not_duplicate_jobs(self):
        payload = {
            "topic": "Durable jobs",
            "target_audience": "engineers",
            "duration_minutes": 1,
            "platform": "youtube",
            "tone": "clear",
            "key_messages": ["state survives restart"],
            "visual_style": "clean",
            "call_to_action": "ship safely",
        }
        first = self.client.post("/api/jobs", json=payload, headers={"Idempotency-Key": "brief-1"})
        second = self.client.post("/api/jobs", json=payload, headers={"Idempotency-Key": "brief-1"})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])
        jobs = job_store.list_jobs(path=api.DATABASE_FILE)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "queued")
        self.assertTrue((api.OUTPUT_ROOT / first.json()["id"] / "brief.yaml").exists())

    def test_sqlite_job_responses_exclude_internal_lease_and_run_fields(self):
        job_store.create_job(
            "private-fields",
            {"id": "private-fields", "topic": "private"},
            output_dir=api.OUTPUT_ROOT / "private-fields",
            path=api.DATABASE_FILE,
        )
        job_store.claim_next_job("worker-secret:123", path=api.DATABASE_FILE)
        response = self.client.get("/api/jobs/private-fields")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            [stage["stage_name"] for stage in payload["stages"]],
            list(job_store.DEFAULT_STAGE_NAMES),
        )
        for key in (
            "lease_owner", "lease_expires_at", "retry_count", "max_retries",
            "next_attempt_at", "run_id", "attempt", "source_digest", "output_dir",
        ):
            self.assertNotIn(key, payload)

    def test_diagnostics_exposes_queue_and_storage_without_secrets(self):
        response = self.client.get("/api/diagnostics")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["database"]["backend"], "sqlite")
        self.assertIn("queue_depth", payload["queue"])
        self.assertIn("free_bytes", payload["storage"])
        self.assertNotIn("token", response.text.lower())

    def test_sqlite_job_lookup_is_not_limited_by_recent_listing_window(self):
        for index in range(51):
            job_id = f"job-{index:03d}"
            job_store.create_job(
                job_id,
                {"id": job_id, "topic": job_id},
                output_dir=api.OUTPUT_ROOT / job_id,
                path=api.DATABASE_FILE,
            )

        oldest = self.client.get("/api/jobs/job-000")
        self.assertEqual(oldest.status_code, 200)
        recent = self.client.get("/api/jobs?limit=50")
        self.assertEqual(recent.status_code, 200)
        self.assertEqual(len(recent.json()), 50)
        self.assertNotIn("job-000", {job["id"] for job in recent.json()})

    def test_sqlite_status_ignores_old_shared_job_root_without_run_id(self):
        job_id = "stale-root"
        job_store.create_job(
            job_id,
            {"id": job_id, "topic": "stale root"},
            output_dir=api.OUTPUT_ROOT / job_id,
            path=api.DATABASE_FILE,
        )
        shared = api.OUTPUT_ROOT / job_id
        shared.mkdir(parents=True)
        (shared / "creative_brief.json").write_text("{}", encoding="utf-8")

        response = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["package_status"], "not_started")

    def test_sqlite_status_resolves_current_run_and_rejects_stale_run(self):
        job_id = "current-run"
        job_store.create_job(
            job_id,
            {"id": job_id, "topic": "current run", "duration_seconds": 60},
            output_dir=api.OUTPUT_ROOT / job_id,
            path=api.DATABASE_FILE,
        )
        claim = job_store.claim_next_job("worker-a", path=api.DATABASE_FILE)
        self.assertIsNotNone(claim)
        assert claim is not None
        job_root = api.OUTPUT_ROOT / job_id
        run_dir = job_root / "runs" / claim.job["run_id"]
        (run_dir / "audio").mkdir(parents=True)
        brief = "topic: current run\n"
        (run_dir / "brief.yaml").write_text(brief, encoding="utf-8")
        source_digest = hashlib.sha256(brief.encode("utf-8")).hexdigest()
        job_store.update_fields(
            job_id,
            {"source_digest": source_digest},
            worker_id="worker-a",
            run_id=claim.job["run_id"],
            path=api.DATABASE_FILE,
        )
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
            path = run_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        atomic_write_json(run_dir / "run_provenance.json", {
            "version": 1,
            "job_id": job_id,
            "attempt": claim.job["attempt"],
            "run_id": claim.job["run_id"],
            "source_digest": source_digest,
            "stages": {},
        })
        stale = job_root / "runs" / "stale-run"
        stale.mkdir(parents=True)
        (stale / "creative_brief.json").write_text("{}", encoding="utf-8")

        response = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["package_status"], "editor_package")

        atomic_write_json(run_dir / "run_provenance.json", {
            "version": 1,
            "job_id": job_id,
            "attempt": claim.job["attempt"],
            "run_id": "stale-run",
            "source_digest": source_digest,
            "stages": {},
        })
        stale_response = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(stale_response.status_code, 200)
        self.assertEqual(stale_response.json()["package_status"], "not_started")

    def test_legacy_import_rejects_unverifiable_completed_job(self):
        job_id = "legacy-completed"
        source = Path(self.tmp.name) / "jobs.json"
        source.write_text(json.dumps({
            job_id: {
                "id": job_id,
                "topic": "legacy package",
                "status": "completed",
                "completed_at": "2026-01-01T00:00:00+00:00",
            }
        }), encoding="utf-8")
        original_app_dir = job_store.APP_DIR
        original_output_root = api.OUTPUT_ROOT
        try:
            job_store.APP_DIR = Path(self.tmp.name) / "legacy-app"
            api.OUTPUT_ROOT = job_store.APP_DIR / "output"
            with self.assertRaises(job_store.InvalidStoreState):
                job_store.import_jobs_json_once(source, path=api.DATABASE_FILE)
            self.assertEqual(job_store.list_jobs(path=api.DATABASE_FILE), [])
        finally:
            job_store.APP_DIR = original_app_dir
            api.OUTPUT_ROOT = original_output_root

    def test_sqlite_missing_topic_is_normalized_for_api_response(self):
        job_store.create_job(
            "missing-topic",
            {"target_audience": "developers"},
            output_dir=api.OUTPUT_ROOT / "missing-topic",
            path=api.DATABASE_FILE,
        )
        response = self.client.get("/api/jobs/missing-topic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["topic"], "")


if __name__ == "__main__":
    unittest.main()
