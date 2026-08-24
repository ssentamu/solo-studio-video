import json
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import job_store
import media_assembly


class LeaseBoundPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state" / "solo.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, job_id="publication-job", **kwargs):
        return job_store.create_job(
            job_id,
            {"id": job_id, "topic": "protocol test"},
            output_dir=Path(self.tmp.name) / "output" / job_id,
            path=self.db,
            **kwargs,
        )

    def test_expired_publication_rejects_callback_before_rename(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", now=100.0, lease_seconds=10, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        temporary = Path(self.tmp.name) / "temporary.mp4"
        final = Path(self.tmp.name) / "final.mp4"
        temporary.write_bytes(b"verified")
        callback_called = []

        def publication():
            callback_called.append(True)
            os.replace(temporary, final)

        with self.assertRaises(job_store.LeaseLost):
            job_store.publish_final_media(
                "publication-job",
                "worker-a",
                claim.job["run_id"],
                publication=publication,
                evidence={"final_video_sha256": "a" * 64},
                path=self.db,
            )

        self.assertFalse(callback_called)
        self.assertTrue(temporary.exists())
        self.assertFalse(final.exists())

    def test_publication_callback_runs_inside_barrier_and_commits_evidence_event(self):
        self.create()
        now = time.time()
        claim = job_store.claim_next_job("worker-a", now=now, lease_seconds=300, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        temporary = Path(self.tmp.name) / "temporary.mp4"
        final = Path(self.tmp.name) / "final.mp4"
        temporary.write_bytes(b"verified")
        observed = []

        def publication():
            observed.append(True)
            os.replace(temporary, final)

        job = job_store.publish_final_media(
            "publication-job",
            "worker-a",
            claim.job["run_id"],
            publication=publication,
            evidence={
                "final_video_sha256": "b" * 64,
                "final_video_duration_seconds": 4.0,
                "final_video_plan_sha256": "c" * 64,
            },
            path=self.db,
        )

        self.assertEqual(observed, [True])
        self.assertTrue(final.exists())
        self.assertFalse(temporary.exists())
        self.assertEqual(job["final_video_sha256"], "b" * 64)
        with job_store.connect(self.db) as connection:
            event = connection.execute(
                "SELECT event_type FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT 1",
                ("publication-job",),
            ).fetchone()
        self.assertEqual(event["event_type"], "final_media_published")

    def test_stale_failure_finalization_does_not_publish_manifest(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", now=100.0, lease_seconds=10, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        manifest = Path(self.tmp.name) / "package_manifest.json"

        def publish(_job):
            manifest.write_text('{"status":"failed"}')
            return {"package_status": "failed"}

        with self.assertRaises(job_store.LeaseLost):
            job_store.finalize_failure(
                "publication-job",
                "worker-a",
                claim.job["run_id"],
                publish_manifest=publish,
                error_code="stage_failed",
                error_message="stale worker",
                retryable=True,
                path=self.db,
            )
        self.assertFalse(manifest.exists())
        self.assertEqual(job_store.get_job("publication-job", self.db)["status"], "running")

    def test_failure_finalization_retries_then_terminal_failure(self):
        self.create(max_retries=1)
        now = time.time()
        first = job_store.claim_next_job("worker-a", now=now, lease_seconds=300, path=self.db)
        self.assertIsNotNone(first)
        assert first is not None
        manifests = []

        def publish(_job):
            manifests.append("failed")
            return {
                "package_status": "failed",
                "has_visuals": False,
                "has_voiceover": False,
                "has_clips": False,
                "has_final_video": False,
                "verified_clips": 0,
            }

        queued = job_store.finalize_failure(
            "publication-job",
            "worker-a",
            first.job["run_id"],
            publish_manifest=publish,
            error_code="timeout",
            error_message="provider timeout",
            retryable=True,
            now=now,
            backoff_seconds=0,
            path=self.db,
        )
        self.assertEqual(queued["status"], "queued")
        second = job_store.claim_next_job("worker-b", now=now + 1, path=self.db)
        self.assertIsNotNone(second)
        assert second is not None
        failed = job_store.finalize_failure(
            "publication-job",
            "worker-b",
            second.job["run_id"],
            publish_manifest=publish,
            error_code="invalid",
            error_message="bad artifact",
            retryable=False,
            now=now + 1,
            path=self.db,
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "invalid")
        self.assertEqual(failed["package_status"], "failed")
        self.assertEqual(len(manifests), 2)

    def test_published_failure_manifest_with_invalid_evidence_fails_closed(self):
        self.create()
        now = time.time()
        claim = job_store.claim_next_job("worker-a", now=now, lease_seconds=300, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        manifest = Path(self.tmp.name) / "package_manifest.json"

        def publish(_job):
            manifest.write_text('{"status":"failed"}')
            return {"package_status": object()}

        with self.assertRaises(job_store.InvalidStoreState):
            job_store.finalize_failure(
                "publication-job",
                "worker-a",
                claim.job["run_id"],
                publish_manifest=publish,
                error_code="stage_failed",
                error_message="bad artifact",
                retryable=True,
                now=now,
                path=self.db,
            )

        job = job_store.get_job("publication-job", self.db)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertTrue(manifest.exists())
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["run_id"], claim.job["run_id"])
        self.assertIsNone(job["lease_owner"])
        self.assertIsNone(job["lease_expires_at"])
        self.assertEqual(job["error_code"], "failure_finalization_required")
        with job_store.connect(self.db) as connection:
            event = connection.execute(
                "SELECT event_type FROM job_events WHERE job_id=? AND event_type=?",
                ("publication-job", "reconciliation_required"),
            ).fetchone()
        self.assertIsNotNone(event)

    def test_worker_routes_sqlite_failure_through_atomic_finalizer(self):
        import worker

        self.create()
        now = time.time()
        claim = job_store.claim_next_job("worker-a", now=now, lease_seconds=300, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        old_configured, old_database, old_worker = worker.DATABASE_CONFIGURED, worker.DATABASE_FILE, worker.CURRENT_WORKER_ID
        output = Path(self.tmp.name) / "failure-output"
        try:
            worker.DATABASE_CONFIGURED = True
            worker.DATABASE_FILE = self.db
            worker.CURRENT_WORKER_ID = "worker-a"
            worker._fail_job("publication-job", output, claim.job, "stage failed")
        finally:
            worker.DATABASE_CONFIGURED, worker.DATABASE_FILE, worker.CURRENT_WORKER_ID = old_configured, old_database, old_worker

        job = job_store.get_job("publication-job", self.db)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["package_status"], "failed")
        self.assertEqual(json.loads((output / "package_manifest.json").read_text())["job"]["status"], "failed")

    def test_callback_failure_persists_visible_reconciliation_error(self):
        self.create()
        now = time.time()
        claim = job_store.claim_next_job("worker-a", now=now, lease_seconds=300, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None

        def publication():
            raise OSError("disk full")

        with self.assertRaises(OSError):
            job_store.publish_final_media(
                "publication-job",
                "worker-a",
                claim.job["run_id"],
                publication=publication,
                evidence={"final_video_sha256": "d" * 64},
                path=self.db,
            )
        job = job_store.get_job("publication-job", self.db)
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["error_code"], "publication_reconciliation_required")
        self.assertIn("reconciliation_error", job)

    def test_publication_callback_sleeping_past_lease_cannot_commit_final_evidence(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", now=time.time(), lease_seconds=1, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        temporary = Path(self.tmp.name) / "temporary.mp4"
        final = Path(self.tmp.name) / "final.mp4"
        temporary.write_bytes(b"verified")

        def publication():
            os.replace(temporary, final)
            time.sleep(1.1)

        with self.assertRaises(job_store.LeaseLost):
            job_store.publish_final_media(
                "publication-job",
                "worker-a",
                claim.job["run_id"],
                publication=publication,
                evidence={"final_video_sha256": "e" * 64},
                path=self.db,
            )

        job = job_store.get_job("publication-job", self.db)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "publication_reconciliation_required")
        self.assertNotIn("final_video_sha256", job)
        self.assertTrue(final.exists())

    def test_rename_success_then_error_is_compensated_when_unpublish_succeeds(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", now=time.time(), lease_seconds=300, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        temporary = Path(self.tmp.name) / "temporary.mp4"
        final = Path(self.tmp.name) / "final.mp4"
        temporary.write_bytes(b"verified")

        def publication():
            os.replace(temporary, final)
            raise OSError("callback failed after rename")

        def unpublish():
            os.replace(final, temporary)

        with self.assertRaises(OSError):
            job_store.publish_final_media(
                "publication-job",
                "worker-a",
                claim.job["run_id"],
                publication=publication,
                rollback_publication=unpublish,
                evidence={"final_video_sha256": "f" * 64},
                path=self.db,
            )

        job = job_store.get_job("publication-job", self.db)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertNotEqual(job["status"], "completed")
        self.assertNotIn("final_video_sha256", job)
        self.assertTrue(temporary.exists())
        self.assertFalse(final.exists())

    def test_failure_callback_sleeping_past_lease_persists_stale_attempt_reconciliation(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", now=time.time(), lease_seconds=1, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        manifest = Path(self.tmp.name) / "package_manifest.json"

        def publish(_job):
            manifest.write_text('{"status":"failed"}')
            time.sleep(1.1)
            return {"package_status": "failed"}

        with self.assertRaises(job_store.LeaseLost):
            job_store.finalize_failure(
                "publication-job",
                "worker-a",
                claim.job["run_id"],
                publish_manifest=publish,
                error_code="stage_failed",
                error_message="stale worker",
                retryable=True,
                path=self.db,
            )

        job = job_store.get_job("publication-job", self.db)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "failure_finalization_required")
        self.assertIsNone(job["next_attempt_at"])
        self.assertTrue(manifest.exists())

    def test_verified_inode_rejects_replacement_immediately_after_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "private"
            destination_dir = root / "public"
            source_dir.mkdir()
            destination_dir.mkdir()
            source = source_dir / "assembled.mp4"
            destination = destination_dir / "video.mp4"
            replacement = root / "replacement.mp4"
            source.write_bytes(b"verified-media")
            replacement.write_bytes(b"replacement-media")
            source_fd = os.open(source, os.O_RDONLY)
            source_dir_fd = os.open(source_dir, os.O_RDONLY | os.O_DIRECTORY)
            destination_dir_fd = os.open(destination_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                source_stat = os.fstat(source_fd)
                request = media_assembly.PublicationRequest(
                    source_dir_fd=source_dir_fd,
                    source_name=source.name,
                    destination_dir_fd=destination_dir_fd,
                    destination_name=destination.name,
                    verified={"sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
                    verified_descriptor=source_fd,
                    verified_inode=(source_stat.st_dev, source_stat.st_ino),
                )
                original_replace = os.replace

                def replace_then_swap(src, dst, **kwargs):
                    original_replace(src, dst, **kwargs)
                    if dst == destination.name and kwargs.get("dst_dir_fd") == destination_dir_fd:
                        original_replace(replacement, destination.name, dst_dir_fd=destination_dir_fd)

                with patch("media_assembly.os.replace", side_effect=replace_then_swap):
                    with self.assertRaises(media_assembly.MediaError):
                        media_assembly.publish_verified_output(request)
            finally:
                os.close(destination_dir_fd)
                os.close(source_dir_fd)
                os.close(source_fd)
            self.assertEqual(destination.read_bytes(), b"replacement-media")
            self.assertFalse(source.exists())

    def test_final_evidence_uses_verified_descriptor_not_post_publication_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "scene_01.mp4"
            output = root / "final" / "video.mp4"
            clip.write_bytes(b"\x00\x00\x00\x18ftypisomclip")
            published = False

            def fake_verify(path, **_kwargs):
                return {
                    "path": str(path),
                    "duration_seconds": 1.0,
                    "streams": [{"codec_type": "video", "codec_name": "h264"}],
                }

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisomassembled")
                return type("Completed", (), {"returncode": 0})()

            def publication(request):
                nonlocal published
                media_assembly.publish_verified_output(request)
                published = True

            original_open = os.open

            def reject_post_publication_open(*args, **kwargs):
                if published and args and args[0] == output.name:
                    raise AssertionError("final media was reopened by pathname after publication")
                return original_open(*args, **kwargs)

            with patch("media_assembly.verify_mp4", side_effect=fake_verify), patch(
                "media_assembly.subprocess.run", side_effect=fake_run
            ), patch("media_assembly.os.open", side_effect=reject_post_publication_open):
                result = media_assembly.assemble_verified_clips(
                    [clip], output, publication_callback=publication
                )

            self.assertTrue(published)
            self.assertEqual(result["sha256"], hashlib.sha256(b"\x00\x00\x00\x18ftypisomassembled").hexdigest())

    def test_fail_job_store_error_writes_reconciliation_marker_and_does_not_hide_state(self):
        import worker

        self.create()
        claim = job_store.claim_next_job("worker-a", now=time.time(), lease_seconds=300, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        old_configured, old_database, old_worker = worker.DATABASE_CONFIGURED, worker.DATABASE_FILE, worker.CURRENT_WORKER_ID
        output = Path(self.tmp.name) / "failure-output"
        try:
            worker.DATABASE_CONFIGURED = True
            worker.DATABASE_FILE = self.db
            worker.CURRENT_WORKER_ID = "worker-a"
            with patch.object(job_store, "finalize_failure", side_effect=job_store.StoreUnavailable("database unavailable")):
                worker._fail_job("publication-job", output, claim.job, "stage failed")
        finally:
            worker.DATABASE_CONFIGURED, worker.DATABASE_FILE, worker.CURRENT_WORKER_ID = old_configured, old_database, old_worker

        marker = json.loads((output / "failure_reconciliation.json").read_text())
        self.assertEqual(marker["kind"], "failure_reconciliation_required")
        self.assertEqual(marker["job_id"], "publication-job")
        self.assertEqual(marker["status"], "reconciliation_required")


if __name__ == "__main__":
    unittest.main()
