import json
import tempfile
import threading
import unittest
from pathlib import Path

import job_store


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state" / "solo.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, job_id="job-1", **kwargs):
        return job_store.create_job(
            job_id,
            {"id": job_id, "topic": "test topic", "duration_seconds": 60},
            output_dir=Path(self.tmp.name) / "output" / job_id,
            path=self.db,
            **kwargs,
        )

    def test_restart_persists_job_and_stage_state(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        job_store.update_stage("job-1", "research", "succeeded", worker_id="worker-a", progress=0.2, path=self.db)
        restored = job_store.get_job("job-1", self.db)
        self.assertEqual(restored["status"], "running")
        self.assertEqual(restored["stages"][0]["status"], "succeeded")
        self.assertEqual(restored["progress"], 0.2)

    def test_legacy_non_string_output_dir_fails_as_invalid_store_state(self):
        base = {"owner_id": "default", "status": "queued", "stage": "research"}
        for value in (False, 123, [], {}, "", "   "):
            with self.subTest(value=value):
                payload = dict(base, output_dir=value)
                with self.assertRaises(job_store.InvalidStoreState):
                    job_store._validate_legacy_record("job-legacy", payload)

    def test_sql_columns_override_creation_payload_after_worker_update(self):
        self.create()
        job_store.claim_next_job("worker-a", path=self.db)
        restored = job_store.update_fields(
            "job-1",
            {"status": "completed", "stage": "done", "progress": 1.0, "error": "finished"},
            worker_id="worker-a",
            path=self.db,
        )
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["stage"], "done")
        self.assertEqual(restored["progress"], 1.0)
        self.assertEqual(restored["error"], "finished")

    def test_idempotency_returns_original_job_without_duplicate(self):
        first, created = self.create(idempotency_key="same-request")
        second, second_created = self.create(job_id="job-2", idempotency_key="same-request")
        self.assertTrue(created)
        self.assertFalse(second_created)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(job_store.list_jobs(path=self.db)), 1)

    def test_two_workers_can_claim_only_one_job(self):
        self.create()
        results = []
        barrier = threading.Barrier(2)

        def claim(worker):
            barrier.wait()
            results.append(job_store.claim_next_job(worker, path=self.db))

        threads = [threading.Thread(target=claim, args=(f"w-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_expired_lease_is_reclaimed(self):
        self.create()
        first = job_store.claim_next_job("worker-a", now=100.0, path=self.db)
        self.assertIsNotNone(first)
        self.assertIsInstance(first.job.get("run_id"), str)
        second = job_store.claim_next_job("worker-b", now=500.0, path=self.db)
        self.assertIsNotNone(second)
        self.assertEqual(second.worker_id, "worker-b")
        self.assertEqual(second.job["attempt"], 2)
        self.assertNotEqual(first.job["run_id"], second.job["run_id"])

    def test_claim_assigns_run_id_and_records_identity_event(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", now=100.0, path=self.db)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.job["attempt"], 1)
        self.assertIsInstance(claim.job["run_id"], str)
        self.assertTrue(claim.job["run_id"])
        with job_store.connect(self.db) as connection:
            event = connection.execute(
                "SELECT payload_json FROM job_events WHERE job_id=? AND event_type='claimed'",
                ("job-1",),
            ).fetchone()
        payload = json.loads(event["payload_json"])
        self.assertEqual(payload["attempt"], 1)
        self.assertEqual(payload["run_id"], claim.job["run_id"])

    def test_retry_requeue_clears_stale_run_until_next_claim(self):
        self.create()
        first = job_store.claim_next_job("worker-a", now=100.0, path=self.db)
        self.assertIsNotNone(first)
        retry = job_store.fail_job(
            "job-1", "worker-a", error_code="timeout", error_message="provider timeout",
            retryable=True, now=100.0, backoff_seconds=5, path=self.db,
        )
        self.assertEqual(retry["status"], "queued")
        self.assertIsNone(retry.get("run_id"))
        second = job_store.claim_next_job("worker-b", now=105.0, path=self.db)
        self.assertIsNotNone(second)
        self.assertEqual(second.job["attempt"], 2)
        self.assertNotEqual(first.job["run_id"], second.job["run_id"])

    def test_expired_lease_counts_toward_retry_limit_and_records_event(self):
        self.create(max_retries=1)
        claim = job_store.claim_next_job("worker-a", now=100.0, path=self.db)
        self.assertIsNotNone(claim)

        self.assertIsNone(job_store.claim_next_job("worker-b", now=500.0, path=self.db))
        expired = job_store.get_job("job-1", path=self.db)
        self.assertIsNotNone(expired)
        assert expired is not None
        self.assertEqual(expired["status"], "failed")
        self.assertEqual(expired["retry_count"], 1)
        self.assertEqual(expired["error_code"], "lease_expired")
        with job_store.connect(self.db) as connection:
            events = connection.execute(
                "SELECT event_type FROM job_events WHERE job_id=?",
                ("job-1",),
            ).fetchall()
        self.assertTrue(any(event["event_type"] == "lease_expired" for event in events))

    def test_startup_reconciliation_counts_expired_lease_toward_retry_limit(self):
        self.create(max_retries=1)
        claim = job_store.claim_next_job("worker-a", now=100.0, path=self.db)
        self.assertIsNotNone(claim)

        self.assertEqual(job_store.reconcile_expired_leases(now=500.0, path=self.db), 1)
        expired = job_store.get_job("job-1", path=self.db)
        self.assertIsNotNone(expired)
        assert expired is not None
        self.assertEqual(expired["status"], "failed")
        self.assertEqual(expired["retry_count"], 1)
        self.assertEqual(expired["error_code"], "lease_expired")

    def test_heartbeat_requires_current_live_owner(self):
        self.create()
        job_store.claim_next_job("worker-a", now=100.0, lease_seconds=100, path=self.db)
        with self.assertRaises(job_store.LeaseLost):
            job_store.heartbeat("job-1", "worker-b", now=110.0, path=self.db)
        renewed = job_store.heartbeat("job-1", "worker-a", now=110.0, path=self.db)
        self.assertEqual(renewed["lease_owner"], "worker-a")
        self.assertGreater(renewed["lease_expires_at"], 110.0)

    def test_retryable_failure_requeues_then_permanent_failure_terminal(self):
        self.create()
        job_store.claim_next_job("worker-a", now=100.0, path=self.db)
        retry = job_store.fail_job(
            "job-1", "worker-a", error_code="timeout", error_message="provider timeout",
            retryable=True, now=100.0, backoff_seconds=5, path=self.db,
        )
        self.assertEqual(retry["status"], "queued")
        self.assertEqual(retry["retry_count"], 1)
        self.assertEqual(retry["next_attempt_at"], 105.0)
        job_store.claim_next_job("worker-b", now=105.0, path=self.db)
        failed = job_store.fail_job(
            "job-1", "worker-b", error_code="invalid", error_message="bad artifact",
            retryable=False, now=110.0, path=self.db,
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "invalid")

    def test_cancellation_is_terminal_and_observable(self):
        self.create()
        job_store.cancel_job("job-1", reason="user requested", path=self.db)
        self.assertTrue(job_store.is_cancelled("job-1", self.db))
        self.assertEqual(job_store.get_job("job-1", self.db)["status"], "cancelled")
        self.assertIsNone(job_store.claim_next_job("worker-a", path=self.db))

    def test_initializing_transition_cannot_resurrect_cancelled_job(self):
        self.create(initial_status="initializing")
        job_store.cancel_job("job-1", reason="user requested", path=self.db)
        updated = job_store.update_fields(
            "job-1",
            {"status": "queued"},
            expected_status="initializing",
            require_not_cancelled=True,
            path=self.db,
        )
        self.assertEqual(updated["status"], "cancelled")
        self.assertIsNotNone(updated["cancelled_at"])

    def test_startup_reconciliation_fails_stale_initializing_jobs(self):
        self.create(initial_status="initializing")
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET updated_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", "job-1"),
            )
            connection.commit()

        self.assertEqual(
            job_store.reconcile_initializing_jobs(now=2000000000.0, max_age_seconds=900, path=self.db),
            1,
        )
        job = job_store.get_job("job-1", self.db)
        if job is None:
            self.fail("reconciled job disappeared")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "initialization_abandoned")
        self.assertIsNotNone(job["completed_at"])

    def test_startup_reconciliation_fails_closed_on_invalid_timestamp(self):
        self.create(initial_status="initializing")
        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET updated_at=? WHERE id=?", ("not-a-timestamp", "job-1"))
            connection.commit()

        self.assertEqual(job_store.reconcile_initializing_jobs(path=self.db), 1)
        job = job_store.get_job("job-1", self.db)
        if job is None:
            self.fail("reconciled job disappeared")
        self.assertEqual(job["status"], "failed")

    def test_import_rejects_malformed_json(self):
        source = Path(self.tmp.name) / "jobs.json"
        source.write_text("{broken")
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.import_jobs_json_once(source, path=self.db)

    def test_import_rejects_malformed_output_dir_values(self):
        for index, output_dir in enumerate((False, 123, [], {}, "", "  ")):
            with self.subTest(output_dir=output_dir):
                source = Path(self.tmp.name) / f"jobs-{index}.json"
                source.write_text(json.dumps({"legacy": {"id": "legacy", "output_dir": output_dir}}))
                database = Path(self.tmp.name) / f"state-{index}" / "solo.sqlite3"
                with self.assertRaises(job_store.InvalidStoreState):
                    job_store.import_jobs_json_once(source, path=database)

    def test_import_rejects_duplicate_json_keys(self):
        source = Path(self.tmp.name) / "jobs.json"
        source.write_text('{"legacy": {"id": "legacy"}, "legacy": {"id": "legacy"}}')
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.import_jobs_json_once(source, path=self.db)

    def test_import_is_one_time_and_refuses_populated_database(self):
        source = Path(self.tmp.name) / "jobs.json"
        source.write_text(json.dumps({"legacy": {"id": "legacy", "topic": "old"}}))
        self.assertEqual(job_store.import_jobs_json_once(source, path=self.db), 1)
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.import_jobs_json_once(source, path=self.db)
        self.assertEqual(job_store.get_job("legacy", self.db)["topic"], "old")

    def test_import_accepts_historical_completed_done_stage(self):
        source = Path(self.tmp.name) / "jobs.json"
        source.write_text(json.dumps({
            "legacy-completed": {
                "id": "legacy-completed",
                "status": "completed",
                "stage": "done",
                "stage_names": None,
            },
        }))

        self.assertEqual(job_store.import_jobs_json_once(source, path=self.db), 1)
        imported = job_store.get_job("legacy-completed", self.db)
        if imported is None:
            self.fail("legacy completed job was not imported")
        self.assertEqual(imported["status"], "completed")
        self.assertEqual(imported["stage"], "done")

    def test_import_rejects_invalid_max_retries(self):
        source = Path(self.tmp.name) / "jobs.json"
        source.write_text(json.dumps({"legacy": {"id": "legacy", "max_retries": 21}}))
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.import_jobs_json_once(source, path=self.db)

    def test_import_rejects_non_integer_max_retries(self):
        source = Path(self.tmp.name) / "jobs.json"
        source.write_text(json.dumps({"legacy": {"id": "legacy", "max_retries": "three"}}))
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.import_jobs_json_once(source, path=self.db)


if __name__ == "__main__":
    unittest.main()
