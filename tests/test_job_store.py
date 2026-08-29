import json
import math
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from unittest.mock import patch

import job_store


class JobStoreTests(unittest.TestCase):
    def test_load_json_rejects_excessive_nesting_as_invalid_store_state(self):
        with self.assertRaises(job_store.InvalidStoreState):
            job_store._load_json("[" * 300 + "0" + "]" * 300, label="job")

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

    def test_custom_stage_graph_is_rejected_until_worker_supports_it(self):
        with self.assertRaises(job_store.InvalidStoreState):
            self.create(stage_names=("custom-a", "custom-b"))

    def test_reserved_stage_names_are_rejected_from_configured_graphs(self):
        for reserved in sorted(job_store.RESERVED_STAGE_NAMES):
            with self.subTest(reserved=reserved), self.assertRaises(job_store.InvalidStoreState):
                job_store._validate_stage_names((reserved, "custom"))

    def test_migration_backfills_missing_stage_rows_in_canonical_order(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute("DELETE FROM job_stages WHERE job_id='job-1'")
            connection.execute("UPDATE jobs SET stage_names_json=NULL WHERE id='job-1'")
            connection.commit()
        job_store.initialize(self.db)
        restored = job_store.get_job("job-1", self.db)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(
            [stage["stage_name"] for stage in restored["stages"]],
            list(job_store.DEFAULT_STAGE_NAMES),
        )
        self.assertTrue(all(stage["status"] == "pending" for stage in restored["stages"]))

    def test_migration_rolls_back_prior_repairs_when_a_later_job_is_invalid(self):
        self.db.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.db)
        try:
            connection.executescript(job_store.SCHEMA)
            timestamp = "2026-01-01T00:00:00+00:00"
            for job_id, graph in (("first", None), ("second", "not-json")):
                connection.execute(
                    "INSERT INTO jobs(id, owner_id, status, current_stage, progress, created_at, updated_at, "
                    "input_json, output_dir, stage_names_json) VALUES(?, ?, 'queued', 'research', 0.0, ?, ?, ?, ?, ?)",
                    (job_id, "default", timestamp, timestamp, json.dumps({"id": job_id}), str(self.tmp.name), graph),
                )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(job_store.InvalidStoreState):
            job_store.initialize(self.db)

        with job_store.connect(self.db) as connection:
            self.assertIsNone(connection.execute("SELECT stage_names_json FROM jobs WHERE id='first'").fetchone()[0])
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM job_stages WHERE job_id='first'").fetchone()[0],
                0,
            )
            self.assertIsNone(
                connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            )

    def test_migration_rebuilds_partial_rows_when_graph_is_already_persisted(self):
        self.create()
        with job_store.connect(self.db) as connection:
            now = datetime.now(timezone.utc)
            created_at = now - timedelta(seconds=60)
            started_at = (now - timedelta(seconds=30)).isoformat()
            completed_at = (now - timedelta(seconds=29)).isoformat()
            connection.execute(
                "UPDATE jobs SET created_at=?, updated_at=? WHERE id='job-1'",
                (created_at.isoformat(), now.isoformat()),
            )
            connection.execute(
                "UPDATE job_stages SET status='succeeded', attempt=2, started_at=?, completed_at=? "
                "WHERE job_id='job-1' AND stage_name='research'",
                (started_at, completed_at),
            )
            connection.execute("DELETE FROM job_stages WHERE job_id='job-1' AND stage_name='editing'")
            connection.commit()
        job_store.initialize(self.db)
        restored = job_store.get_job("job-1", self.db)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual([stage["stage_name"] for stage in restored["stages"]], list(job_store.DEFAULT_STAGE_NAMES))
        research = next(stage for stage in restored["stages"] if stage["stage_name"] == "research")
        editing = next(stage for stage in restored["stages"] if stage["stage_name"] == "editing")
        self.assertEqual(research["status"], "succeeded")
        self.assertEqual(research["attempt"], 2)
        self.assertEqual(editing["status"], "pending")

    def test_persisted_stage_timestamps_must_follow_configured_order(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE job_stages SET status='succeeded', attempt=1, "
                "started_at=?, completed_at=? WHERE job_id='job-1' AND stage_name=?",
                ("2026-01-02T00:00:00+00:00", "2026-01-02T00:00:01+00:00", "research"),
            )
            connection.execute(
                "UPDATE job_stages SET status='succeeded', attempt=1, "
                "started_at=?, completed_at=? WHERE job_id='job-1' AND stage_name=?",
                ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", "script"),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", self.db)

    def test_pending_stage_transition_cannot_jump_or_rewind(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_stage(
                "job-1", "assembly", "pending", worker_id="worker-a", run_id=claim.job["run_id"], path=self.db,
            )
        for stage in job_store.DEFAULT_STAGE_NAMES[:-1]:
            job_store.update_stage(
                "job-1", stage, "running", worker_id="worker-a", run_id=claim.job["run_id"], path=self.db,
            )
            job_store.update_stage(
                "job-1", stage, "succeeded", worker_id="worker-a", run_id=claim.job["run_id"], path=self.db,
            )
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_stage(
                "job-1", "research", "pending", worker_id="worker-a", run_id=claim.job["run_id"], path=self.db,
            )

    def test_active_job_with_all_terminal_stages_is_rejected(self):
        self.create()
        with job_store.connect(self.db) as connection:
            now = datetime.now(timezone.utc)
            created_at = now - timedelta(seconds=60)
            connection.execute(
                "UPDATE jobs SET status='queued', current_stage=?, created_at=?, updated_at=? WHERE id='job-1'",
                (job_store.DEFAULT_STAGE_NAMES[-1], created_at.isoformat(), now.isoformat()),
            )
            for stage_name in job_store.DEFAULT_STAGE_NAMES:
                connection.execute(
                    "UPDATE job_stages SET status='succeeded', attempt=1, started_at=?, completed_at=? "
                    "WHERE job_id='job-1' AND stage_name=?",
                    (created_at.isoformat(), created_at.isoformat(), stage_name),
                )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", self.db)
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.claim_next_job("worker-a", path=self.db)

    def mark_all_stages_succeeded(self):
        with job_store.connect(self.db) as connection:
            now = datetime.now(timezone.utc)
            created_at = now - timedelta(seconds=60)
            connection.execute(
                "UPDATE jobs SET created_at=?, updated_at=? WHERE id='job-1'",
                (created_at.isoformat(), now.isoformat()),
            )
            for index, stage_name in enumerate(job_store.DEFAULT_STAGE_NAMES):
                start = (now - timedelta(seconds=52 - index * 2)).isoformat()
                completed = (now - timedelta(seconds=51 - index * 2)).isoformat()
                connection.execute(
                    "UPDATE job_stages SET status='succeeded', attempt=1, started_at=?, completed_at=? "
                    "WHERE job_id='job-1' AND stage_name=?",
                    (start, completed, stage_name),
                )
            connection.execute(
                "UPDATE jobs SET updated_at=? WHERE id='job-1'",
                (now.isoformat(),),
            )
            connection.commit()

    def test_restart_persists_job_and_stage_state(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        job_store.update_stage("job-1", "research", "succeeded", worker_id="worker-a", run_id=claim.job["run_id"], progress=0.2, path=self.db)
        restored = job_store.get_job("job-1", self.db)
        self.assertEqual(restored["status"], "running")
        self.assertEqual(restored["stages"][0]["status"], "succeeded")
        self.assertEqual(restored["progress"], 0.2)

    def test_unsafe_stage_update_is_rejected_before_commit(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_stage(
                "job-1", "\nforged", "running", worker_id="worker-a", run_id=claim.job["run_id"], path=self.db,
            )
        with job_store.connect(self.db) as connection:
            row = connection.execute("SELECT current_stage FROM jobs WHERE id='job-1'").fetchone()
            unsafe_stage_count = connection.execute(
                "SELECT COUNT(*) AS count FROM job_stages WHERE job_id='job-1' AND stage_name=?", ("\nforged",),
            ).fetchone()["count"]
        self.assertEqual(row["current_stage"], "research")
        self.assertEqual(unsafe_stage_count, 0)

    def test_unsafe_persisted_stage_state_fails_closed(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO job_stages(job_id, stage_name, status, attempt) VALUES('job-1', ?, 'pending', 0)",
                ("\nunsafe",),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", self.db)

    def test_malformed_persisted_payload_fails_closed_on_read_and_queue_snapshot(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET input_json=? WHERE id='job-1'",
                (json.dumps({"id": "job-1", "duration_seconds": "60"}),),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", self.db)
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.queue_snapshot(path=self.db)

    def test_nonstandard_json_numeric_payload_fails_closed(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET input_json=? WHERE id='job-1'",
                ('{"id":"job-1","untrusted":Infinity}',),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", self.db)
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.queue_snapshot(path=self.db)

    def test_generic_worker_update_rejects_unknown_stage(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields(
                "job-1", {"current_stage": "attacker-stage"}, worker_id="worker-a",
                run_id=claim.job["run_id"], path=self.db,
            )

    def test_completion_requires_publication_evidence_and_done_progress(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.complete_job("job-1", "worker-a", run_id=claim.job["run_id"], path=self.db)
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields(
                "job-1",
                {
                    "status": "completed", "current_stage": "done", "progress": 1.0,
                    "completed_at": "2026-08-26T10:00:00+00:00",
                    "final_video_sha256": "0" * 64,
                    "final_video_plan_sha256": "1" * 64,
                    "final_video_duration_seconds": 1.0,
                },
                worker_id="worker-a", run_id=claim.job["run_id"], path=self.db,
            )

    def test_generic_worker_update_rejects_nonadjacent_stage_jump(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields(
                "job-1", {"current_stage": "assembly"}, worker_id="worker-a",
                run_id=claim.job["run_id"], path=self.db,
            )

    def test_durable_job_rejects_global_application_output_root(self):
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.create_job(
                "global-root-job",
                {"id": "global-root-job", "topic": "test", "duration_seconds": 60},
                output_dir=job_store.APP_DIR / "output" / "global-root-job",
                path=self.db,
            )

    def test_stage_progress_cannot_regress(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        job_store.update_stage(
            "job-1", "research", "succeeded", worker_id="worker-a", run_id=claim.job["run_id"],
            progress=0.8, path=self.db,
        )
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_stage(
                "job-1", "script", "running", worker_id="worker-a", run_id=claim.job["run_id"],
                progress=0.1, path=self.db,
            )

    def test_legacy_non_string_output_dir_fails_as_invalid_store_state(self):
        base = {"owner_id": "default", "status": "queued", "stage": "research"}
        for value in (False, 123, [], {}, "", "   "):
            with self.subTest(value=value):
                payload = dict(base, output_dir=value)
                with self.assertRaises(job_store.InvalidStoreState):
                    job_store._validate_legacy_record("job-legacy", payload)

    def test_legacy_failed_and_cancelled_stage_markers_are_non_overlapping(self):
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                database = Path(self.tmp.name) / status / "solo.sqlite3"
                source = Path(self.tmp.name) / f"{status}.json"
                source.write_text(json.dumps({
                    "job-legacy": {
                        "id": "job-legacy",
                        "status": status,
                        "stage": "research",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:10+00:00",
                        "error": "legacy failure" if status == "failed" else None,
                    },
                }))
                self.assertEqual(job_store.import_jobs_json_once(source, path=database), 1)
                restored = job_store.get_job("job-legacy", database)
                self.assertIsNotNone(restored)
                assert restored is not None
                self.assertEqual(restored["status"], status)
                self.assertTrue(all(
                    stage["status"] == "skipped"
                    and stage["started_at"] == "2026-01-01T00:00:00+00:00"
                    and stage["completed_at"] == "2026-01-01T00:00:00+00:00"
                    for stage in restored["stages"]
                ))

    def test_sql_columns_override_creation_payload_after_worker_update(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        restored = job_store.update_fields(
            "job-1",
            {"status": "running", "stage": "research", "progress": 0.5, "error": "still running"},
            worker_id="worker-a",
            run_id=claim.job["run_id"],
            path=self.db,
        )
        self.assertEqual(restored["status"], "running")
        self.assertEqual(restored["stage"], "research")
        self.assertEqual(restored["progress"], 0.5)
        self.assertEqual(restored["error"], "still running")

    def test_generic_update_rejects_terminal_state_without_contract(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        for fields in (
            {"status": "completed", "completed_at": "2026-01-01T00:00:00+00:00"},
            {"status": "cancelled"},
        ):
            with self.subTest(fields=fields), self.assertRaises(job_store.InvalidStoreState):
                job_store.update_fields(
                    "job-1", fields, worker_id="worker-a", run_id=claim.job["run_id"], path=self.db,
                )

    def test_update_stage_rejects_unknown_configured_stage(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_stage(
                "job-1", "unknown", "running", worker_id="worker-a", run_id=claim.job["run_id"], path=self.db,
            )

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
        first = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(first)
        self.assertIsInstance(first.job.get("run_id"), str)
        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET lease_expires_at=0 WHERE id='job-1'")
            connection.commit()
        second = job_store.claim_next_job("worker-b", path=self.db)
        self.assertIsNotNone(second)
        self.assertEqual(second.worker_id, "worker-b")
        self.assertEqual(second.job["attempt"], 2)
        self.assertNotEqual(first.job["run_id"], second.job["run_id"])

    def test_claim_assigns_run_id_and_records_identity_event(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
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
        first = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(first)
        assert first is not None
        retry = job_store.fail_job(
            "job-1", "worker-a", error_code="timeout", error_message="provider timeout",
            retryable=True, run_id=first.job["run_id"], backoff_seconds=0, path=self.db,
        )
        self.assertEqual(retry["status"], "queued")
        self.assertIsNone(retry.get("run_id"))
        second = job_store.claim_next_job("worker-b", path=self.db)
        self.assertIsNotNone(second)
        self.assertEqual(second.job["attempt"], 2)
        self.assertNotEqual(first.job["run_id"], second.job["run_id"])

    def test_legacy_failure_rejects_nonfinite_and_out_of_range_backoff(self):
        for value in (math.nan, math.inf, -1.0, 3600.1):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "state" / "solo.sqlite3"
                job_store.create_job(
                    "invalid-backoff",
                    {"id": "invalid-backoff", "topic": "test", "duration_seconds": 60},
                    output_dir=Path(tmp) / "output" / "invalid-backoff",
                    path=db,
                )
                claim = job_store.claim_next_job("worker-a", path=db)
                self.assertIsNotNone(claim)
                assert claim is not None
                with self.assertRaises(job_store.InvalidStoreState):
                    job_store.fail_job(
                        "invalid-backoff",
                        "worker-a",
                        error_code="provider_error",
                        error_message="invalid retry delay",
                        retryable=True,
                        run_id=claim.job["run_id"],
                        now=100.0,
                        backoff_seconds=value,
                        path=db,
                    )

    def test_json_persistence_rejects_nonfinite_numbers(self):
        with self.assertRaises(job_store.InvalidStoreState):
            job_store._json({"duration": math.nan})
        with self.assertRaises(job_store.InvalidStoreState):
            job_store._json({"duration": math.inf})

    def test_timestamp_persistence_requires_timezone_aware_iso_values(self):
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.create_job(
                "bad-created-at",
                {"id": "bad-created-at", "created_at": "not-a-timestamp"},
                output_dir=Path(self.tmp.name) / "output" / "out",
                path=self.db,
            )
        self.create()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields("job-1", {"completed_at": "not-a-timestamp"}, path=self.db)
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields("job-1", {"cancelled_at": "not-a-timestamp"}, path=self.db)

    def test_stage_update_requires_worker_and_run_identity(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_stage(
                "job-1", "research", "running", worker_id=cast(str, None), run_id=cast(str, None), path=self.db,
            )

    def test_failure_ignores_stale_caller_clock_for_lease_validation(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", lease_seconds=10, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        failed = job_store.fail_job(
            "job-1", "worker-a", error_code="stale", error_message="stale attempt",
            retryable=False, run_id=claim.job["run_id"], now=0.0, path=self.db,
        )
        self.assertEqual(failed["status"], "failed")

    def test_known_payload_numbers_are_validated_before_persistence(self):
        for field, value in (("duration_seconds", "NaN"), ("chapters", "3"), ("scenes", True), ("progress", "bad")):
            with self.subTest(field=field):
                with self.assertRaises(job_store.InvalidStoreState):
                    job_id = f"invalid-payload-{field}"
                    job_store.create_job(
                        job_id,
                        {"id": job_id, "topic": "test", field: value},
                        output_dir=Path(self.tmp.name) / "output" / job_id,
                        path=self.db,
                    )

    def test_payload_number_validation_applies_to_updates(self):
        for field, value in (("duration_seconds", True), ("chapters", "not-int"), ("scenes", "not-int"), ("progress", "bad")):
            with self.subTest(field=field):
                job_id = f"update-payload-{field}"
                job_store.create_job(job_id, {"id": job_id}, output_dir=Path(self.tmp.name) / "output" / job_id, path=self.db)
                with self.assertRaises(job_store.InvalidStoreState):
                    job_store.update_fields(job_id, {field: value}, path=self.db)

    def test_malformed_persisted_lifecycle_values_fail_closed(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET attempt=?, lease_expires_at=? WHERE id=?", ("not-int", "not-time", "job-1"))
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.claim_next_job("worker-a", path=self.db)

    def test_failed_row_with_terminal_timestamps_fails_closed(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET status='failed', completed_at=?, cancelled_at=? WHERE id=?",
                ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", "job-1"),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)

    def test_running_row_with_unsafe_run_id_fails_closed(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET run_id='../escape' WHERE id=?", ("job-1",))
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)

    def test_null_running_lease_is_terminally_reconciled(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET lease_expires_at=NULL WHERE id=?", ("job-1",))
            connection.commit()
        self.assertEqual(job_store.reconcile_expired_leases(path=self.db), 1)
        repaired = job_store.get_job("job-1", path=self.db)
        self.assertIsNotNone(repaired)
        assert repaired is not None
        self.assertEqual(repaired["status"], "failed")
        self.assertEqual(repaired["error_code"], "invalid_persisted_state")

    def test_malformed_read_state_fails_closed(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET status=?, progress=?, retry_count=?, attempt=?, created_at=? WHERE id=?",
                ("bogus", 9.0, 99, -1, "not-a-timestamp", "job-1"),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.list_jobs(path=self.db)

    def test_heartbeat_rejects_malformed_persisted_lifecycle(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET progress=? WHERE id=?", (9.0, "job-1"))
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.heartbeat("job-1", "worker-a", run_id=claim.job["run_id"], path=self.db)

    def test_stage_corruption_fails_closed_before_arithmetic(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE job_stages SET attempt=? WHERE job_id=? AND stage_name=?", ("bad", "job-1", "research"))
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_stage("job-1", "research", "running", worker_id="worker-a", run_id=claim.job["run_id"], path=self.db)

    def test_final_video_duration_payload_update_requires_number(self):
        self.create()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields("job-1", {"final_video_duration_seconds": "bad"}, path=self.db)

    def test_lifecycle_numeric_strings_and_unsafe_stage_are_rejected(self):
        self.create()
        invalid_fields = (
            {"progress": "0.5"},
            {"next_attempt_at": "12.5"},
            {"stage": 123},
            {"current_stage": "\nforged"},
        )
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                with self.assertRaises(job_store.InvalidStoreState):
                    job_store.update_fields("job-1", fields, path=self.db)

    def test_update_fields_rejects_invalid_mutation_values(self):
        invalid_fields = (
            {"progress": math.nan},
            {"progress": 1.1},
            {"next_attempt_at": math.inf},
            {"retry_count": math.inf},
            {"retry_count": True},
            {"status": "not-a-status"},
        )
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                with tempfile.TemporaryDirectory() as tmp:
                    db = Path(tmp) / "state" / "solo.sqlite3"
                    job_store.create_job("invalid-fields", {"id": "invalid-fields"}, output_dir=Path(tmp) / "output" / "invalid-fields", path=db)
                    claim = job_store.claim_next_job("worker-a", path=db)
                    self.assertIsNotNone(claim)
                    assert claim is not None
                    with self.assertRaises(job_store.InvalidStoreState):
                        job_store.update_fields(
                            "invalid-fields",
                            fields,
                            worker_id="worker-a",
                            run_id=claim.job["run_id"],
                            path=db,
                        )

    def test_update_fields_fences_reused_worker_with_run_id(self):
        self.create(max_retries=3)
        first = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(first)
        assert first is not None
        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET lease_expires_at=0 WHERE id='job-1'")
            connection.commit()
        second = job_store.claim_next_job("worker-b", path=self.db)
        self.assertIsNotNone(second)
        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET lease_expires_at=0 WHERE id='job-1'")
            connection.commit()
        second = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertNotEqual(first.job["run_id"], second.job["run_id"])
        with self.assertRaises(job_store.LeaseLost):
            job_store.update_fields(
                "job-1",
                {"progress": 0.5},
                worker_id="worker-a",
                run_id=first.job["run_id"],
                path=self.db,
            )

    def test_expired_lease_counts_toward_retry_limit_and_records_event(self):
        self.create(max_retries=1)
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)

        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET lease_expires_at=0 WHERE id='job-1'")
            connection.commit()
        retry_claim = job_store.claim_next_job("worker-b", path=self.db)
        self.assertIsNotNone(retry_claim)
        assert retry_claim is not None
        self.assertEqual(retry_claim.job["retry_count"], 1)
        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET lease_expires_at=0 WHERE id='job-1'")
            connection.commit()
        job_store.reconcile_expired_leases(path=self.db)
        expired = job_store.get_job("job-1", path=self.db)
        self.assertIsNotNone(expired)
        assert expired is not None
        self.assertEqual(expired["status"], "failed")
        self.assertEqual(expired["retry_count"], 2)
        self.assertEqual(expired["error_code"], "lease_expired")
        with job_store.connect(self.db) as connection:
            events = connection.execute(
                "SELECT event_type FROM job_events WHERE job_id=?",
                ("job-1",),
            ).fetchall()
        self.assertTrue(any(event["event_type"] == "lease_expired" for event in events))

    def test_retry_exhaustion_keeps_terminal_overflow_readable(self):
        self.create(max_retries=20)
        for expiration in range(21):
            claim = job_store.claim_next_job(
                f"worker-{expiration}", lease_seconds=1, path=self.db
            )
            self.assertIsNotNone(claim)
            with job_store.connect(self.db) as connection:
                connection.execute("UPDATE jobs SET lease_expires_at=0 WHERE id='job-1'")
                connection.commit()
            self.assertEqual(job_store.reconcile_expired_leases(path=self.db), 1)
        exhausted = job_store.get_job("job-1", path=self.db)
        self.assertIsNotNone(exhausted)
        assert exhausted is not None
        self.assertEqual(exhausted["status"], "failed")
        self.assertEqual(exhausted["retry_count"], 21)

    def test_startup_reconciliation_counts_expired_lease_toward_retry_limit(self):
        self.create(max_retries=1)
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)

        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET lease_expires_at=0 WHERE id='job-1'")
            connection.commit()
        self.assertEqual(job_store.reconcile_expired_leases(path=self.db), 1)
        expired = job_store.get_job("job-1", path=self.db)
        self.assertIsNotNone(expired)
        assert expired is not None
        self.assertEqual(expired["status"], "queued")
        self.assertEqual(expired["retry_count"], 1)
        self.assertEqual(expired["error_code"], "lease_expired")

    def test_retry_reset_clears_prior_stage_artifacts(self):
        self.create(max_retries=2)
        claim = job_store.claim_next_job("worker-a", lease_seconds=1, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        job_store.update_stage(
            "job-1", "research", "succeeded", worker_id="worker-a", run_id=claim.job["run_id"],
            artifact={"attempt": 1}, progress=0.2, path=self.db,
        )
        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET lease_expires_at=0 WHERE id='job-1'")
            connection.commit()
        retry_claim = job_store.claim_next_job("worker-b", path=self.db)
        self.assertIsNotNone(retry_claim)
        with job_store.connect(self.db) as connection:
            artifact, next_attempt_at = connection.execute(
                "SELECT artifact_json, next_attempt_at FROM job_stages JOIN jobs ON jobs.id=job_stages.job_id "
                "WHERE job_stages.job_id='job-1' AND stage_name='research'"
            ).fetchone()
        self.assertIsNone(artifact)
        self.assertIsNone(next_attempt_at)

    def test_database_owned_output_root_symlink_is_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        output_root = Path(self.tmp.name) / "output"
        output_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.create_job(
                "symlink-root", {"id": "symlink-root"},
                output_dir=output_root / "symlink-root", path=self.db,
            )

    def test_heartbeat_requires_current_live_owner(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", lease_seconds=100, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with self.assertRaises(job_store.LeaseLost):
            job_store.heartbeat("job-1", "worker-b", run_id=claim.job["run_id"], path=self.db)
        renewed = job_store.heartbeat("job-1", "worker-a", run_id=claim.job["run_id"], path=self.db)
        self.assertEqual(renewed["lease_owner"], "worker-a")
        self.assertGreater(renewed["lease_expires_at"], time.time())

    def test_retryable_failure_requeues_then_permanent_failure_terminal(self):
        self.create()
        first = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(first)
        assert first is not None
        retry = job_store.fail_job(
            "job-1", "worker-a", error_code="timeout", error_message="provider timeout",
            retryable=True, run_id=first.job["run_id"], backoff_seconds=0, path=self.db,
        )
        self.assertEqual(retry["status"], "queued")
        self.assertEqual(retry["retry_count"], 1)
        self.assertIsNotNone(retry["next_attempt_at"])
        second = job_store.claim_next_job("worker-b", path=self.db)
        self.assertIsNotNone(second)
        assert second is not None
        failed = job_store.fail_job(
            "job-1", "worker-b", error_code="invalid", error_message="bad artifact",
            retryable=False, run_id=second.job["run_id"], path=self.db,
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "invalid")

    def test_retry_claim_resets_prior_stage_execution_state(self):
        self.create()
        first = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(first)
        assert first is not None
        job_store.update_stage(
            "job-1", "research", "succeeded", worker_id="worker-a", run_id=first.job["run_id"],
            progress=0.2, path=self.db,
        )
        retry = job_store.fail_job(
            "job-1", "worker-a", error_code="timeout", error_message="retry",
            retryable=True, run_id=first.job["run_id"], backoff_seconds=0.0, path=self.db,
        )
        self.assertEqual(retry["status"], "queued")
        second = job_store.claim_next_job("worker-b", path=self.db)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.job["progress"], 0.0)
        self.assertIsNone(second.job["error_code"])
        self.assertIsNone(second.job["error"])
        self.assertEqual(second.job["stages"][0]["status"], "pending")
        running = job_store.update_stage(
            "job-1", "research", "running", worker_id="worker-b", run_id=second.job["run_id"],
            progress=0.1, path=self.db,
        )
        self.assertEqual(running["stages"][0]["status"], "running")

    def test_sqlite_output_dir_is_confined_to_database_output_root(self):
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.create_job(
                "unsafe-output", {"id": "unsafe-output"}, output_dir="/etc", path=self.db,
            )

    def test_sqlite_output_dir_rejects_falsey_and_cross_job_paths(self):
        for index, value in enumerate((False, 0, "", [], {})):
            with self.subTest(value=value):
                with self.assertRaises(job_store.InvalidStoreState):
                    job_store.create_job(
                        f"falsey-{index}", {"id": f"falsey-{index}"}, output_dir=cast(Any, value), path=self.db,
                    )
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.create_job(
                "cross-job", {"id": "cross-job"},
                output_dir=Path(self.tmp.name) / "output" / "another-job", path=self.db,
            )

    def test_lease_bound_mutations_recheck_clock_after_database_initialization(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", lease_seconds=1, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        original_initialize = job_store.initialize

        def delayed_initialize(path=None):
            time.sleep(1.2)
            return original_initialize(path)

        with patch.object(job_store, "initialize", side_effect=delayed_initialize):
            with self.assertRaises(job_store.LeaseLost):
                job_store.heartbeat("job-1", "worker-a", run_id=claim.job["run_id"], path=self.db)

        claim = job_store.claim_next_job("worker-b", lease_seconds=1, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with patch.object(job_store, "initialize", side_effect=delayed_initialize):
            with self.assertRaises(job_store.LeaseLost):
                job_store.update_stage(
                    "job-1", "research", "running", worker_id="worker-b", run_id=claim.job["run_id"], path=self.db,
                )

        claim = job_store.claim_next_job("worker-c", lease_seconds=1, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with patch.object(job_store, "initialize", side_effect=delayed_initialize):
            with self.assertRaises(job_store.LeaseLost):
                job_store.fail_job(
                    "job-1", "worker-c", error_code="timeout", error_message="expired",
                    retryable=True, run_id=claim.job["run_id"], path=self.db,
                )

        claim = job_store.claim_next_job("worker-d", lease_seconds=1, path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with patch.object(job_store, "initialize", side_effect=delayed_initialize):
            with self.assertRaises(job_store.LeaseLost):
                job_store.update_fields(
                    "job-1", {"progress": 0.1}, worker_id="worker-d",
                    run_id=claim.job["run_id"], path=self.db,
                )

    def test_cancellation_is_terminal_and_observable(self):
        self.create()
        job_store.cancel_job("job-1", owner_id=job_store.DEFAULT_OWNER_ID, reason="user requested", path=self.db)
        self.assertTrue(job_store.is_cancelled("job-1", self.db))
        self.assertEqual(job_store.get_job("job-1", self.db)["status"], "cancelled")
        self.assertIsNone(job_store.claim_next_job("worker-a", path=self.db))

    def test_cancellation_does_not_mutate_editor_package(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        self.mark_all_stages_succeeded()
        completed_at = job_store._now_iso()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields(
                "job-1",
                {
                    "status": "editor_package",
                    "current_stage": "done",
                    "progress": 1.0,
                    "completed_at": completed_at,
                    "package_status": "editor_package",
                },
                worker_id="worker-a",
                run_id=claim.job["run_id"],
                path=self.db,
            )
        cancelled = job_store.cancel_job("job-1", owner_id=job_store.DEFAULT_OWNER_ID, path=self.db)
        self.assertEqual(cancelled["status"], "cancelled")

    def test_terminal_editor_package_rejects_generic_mutation(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        self.mark_all_stages_succeeded()
        completed_at = job_store._now_iso()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields(
                "job-1",
                {
                    "status": "editor_package",
                    "current_stage": "done",
                    "progress": 1.0,
                    "completed_at": completed_at,
                    "package_status": "editor_package",
                },
                worker_id="worker-a",
                run_id=claim.job["run_id"],
                path=self.db,
            )
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields("job-1", {"error_message": "forged"}, path=self.db)
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", self.db)

    def test_invalid_terminal_timestamp_mutation_is_rolled_back(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET status='running', lease_owner='worker-a', run_id='run-a', "
                "lease_expires_at=?, completed_at=? WHERE id='job-1'",
                (time.time() + 60, "2026-01-01T00:00:00+00:00"),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.cancel_job("job-1", owner_id=job_store.DEFAULT_OWNER_ID, path=self.db)
        with job_store.connect(self.db) as connection:
            row = connection.execute(
                "SELECT status, completed_at, cancelled_at FROM jobs WHERE id='job-1'",
            ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["completed_at"], "2026-01-01T00:00:00+00:00")
        self.assertIsNone(row["cancelled_at"])

    def test_initializing_transition_cannot_resurrect_cancelled_job(self):
        self.create(initial_status="initializing")
        job_store.cancel_job("job-1", owner_id=job_store.DEFAULT_OWNER_ID, reason="user requested", path=self.db)
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

    def test_claim_rejects_far_future_caller_clock(self):
        self.create()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.claim_next_job("worker-a", now=1_000_000_000_000.0, path=self.db)

    def test_startup_reconciliation_fails_invalid_initializing_output_path(self):
        self.create(initial_status="initializing")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET output_dir=? WHERE id=?",
                (str(outside), "job-1"),
            )
            connection.commit()

        self.assertEqual(job_store.reconcile_initializing_jobs(path=self.db), 1)
        with job_store.connect(self.db) as connection:
            row = connection.execute(
                "SELECT status, error_code FROM jobs WHERE id=?", ("job-1",)
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "initialization_abandoned")

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

    def test_imported_running_job_is_executable_after_claim(self):
        source = Path(self.tmp.name) / "running-jobs.json"
        source.write_text(json.dumps({
            "legacy-running": {
                "id": "legacy-running",
                "status": "running",
                "stage": "research",
                "progress": 0.2,
                "topic": "resume me",
            },
        }))
        self.assertEqual(job_store.import_jobs_json_once(source, path=self.db), 1)
        imported = job_store.get_job("legacy-running", path=self.db)
        self.assertIsNotNone(imported)
        assert imported is not None
        self.assertEqual(imported["status"], "queued")
        self.assertTrue(all(stage["status"] == "pending" for stage in imported["stages"]))
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        job_store.update_stage(
            "legacy-running", "research", "running", worker_id="worker-a", run_id=claim.job["run_id"], path=self.db,
        )
        progressed = job_store.update_stage(
            "legacy-running", "research", "succeeded", worker_id="worker-a", run_id=claim.job["run_id"], path=self.db,
        )
        self.assertEqual(progressed["current_stage"], "script")

    def test_import_rejects_historical_completed_without_final_evidence(self):
        source = Path(self.tmp.name) / "jobs.json"
        source.write_text(json.dumps({
            "legacy-completed": {
                "id": "legacy-completed",
                "status": "completed",
                "stage": "done",
                "stage_names": None,
            },
        }))

        with self.assertRaises(job_store.InvalidStoreState):
            job_store.import_jobs_json_once(source, path=self.db)
        self.assertEqual(job_store.list_jobs(path=self.db), [])

    def test_import_accepts_exact_production_legacy_records(self):
        source = Path(self.tmp.name) / "jobs.json"
        source.write_text(json.dumps({
            "68539c757ce7": {
                "id": "68539c757ce7",
                "topic": "How AI Coding Agents Are Reshaping Software Engineering in 2026",
                "target_audience": "senior engineers, engineering managers, CTOs, technical founders",
                "duration_seconds": 900,
                "platform": "youtube",
                "tone": "educational",
                "key_messages": ["AI coding agents now handle most junior developer tasks"],
                "visual_style": "dark mode tech aesthetic, code on screens",
                "call_to_action": "Subscribe for weekly deep dives.",
                "status": "failed",
                "progress": 0.92,
                "stage": "assembly",
                "format": "long",
                "chapters": 12,
                "scenes": 36,
                "created_at": "2026-08-04T18:30:45.923924+00:00",
                "completed_at": None,
                "error": "name 'timezone' is not defined",
                "has_visuals": True,
                "has_voiceover": True,
                "stage_names": None,
            },
        }))

        self.assertEqual(job_store.import_jobs_json_once(source, path=self.db), 1)
        failed = job_store.get_job("68539c757ce7", self.db)
        if failed is None:
            self.fail("legacy failed job was not imported")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["stage"], "assembly")
        self.assertEqual(failed["progress"], 0.92)
        self.assertEqual(failed["error"], "name 'timezone' is not defined")
        self.assertIsNone(failed["completed_at"])


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

    def test_guarded_terminal_paths_reject_private_completion_and_timestamp_conflict(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", now=time.time(), path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        with self.assertRaises(job_store.InvalidStoreState):
            job_store._finish_job("job-1", "worker-a", run_id=claim.job["run_id"], status="completed", path=self.db)
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields(
                "job-1",
                {
                    "status": "cancelled",
                    "current_stage": "research",
                    "cancelled_at": "2026-01-01T00:00:00+00:00",
                    "completed_at": "2026-01-01T00:00:00+00:00",
                },
                worker_id="worker-a",
                run_id=claim.job["run_id"],
                path=self.db,
            )
    def test_persisted_terminal_rows_require_semantic_lifecycle_invariants(self):
        job_store.create_job(
            "job-1",
            {
                "id": "job-1",
                "topic": "test topic",
            },
            output_dir=Path(self.tmp.name) / "output" / "job-1",
            path=self.db,
        )
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET input_json=? WHERE id='job-1'",
                (json.dumps({
                    "id": "job-1", "topic": "test topic", "package_status": "final_video_ready",
                    "final_video_sha256": "a" * 64, "final_video_plan_sha256": "b" * 64,
                    "final_video_duration_seconds": 60.0,
                }),),
            )
            connection.commit()
        corruptions = (
            {"status": "completed", "progress": 0.5, "completed_at": None, "cancelled_at": None},
            {"status": "completed", "progress": 1.0, "completed_at": "2026-01-01T00:00:00+00:00", "cancelled_at": "2026-01-01T00:00:00+00:00"},
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                with job_store.connect(self.db) as connection:
                    connection.execute(
                        "UPDATE jobs SET status=?, progress=?, current_stage='done', completed_at=?, cancelled_at=? WHERE id='job-1'",
                        (corruption["status"], corruption["progress"], corruption["completed_at"], corruption["cancelled_at"]),
                    )
                    connection.commit()
                with self.assertRaises(job_store.InvalidStoreState):
                    job_store.get_job("job-1", path=self.db)
                with job_store.connect(self.db) as connection:
                    connection.execute(
                        "UPDATE jobs SET status='queued', progress=0.0, current_stage='research', completed_at=NULL, cancelled_at=NULL WHERE id='job-1'"
                    )
                    connection.commit()

    def test_creation_rejects_publication_owned_evidence(self):
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.create_job(
                "forged-evidence",
                {
                    "id": "forged-evidence",
                    "topic": "test",
                    "final_video_sha256": "a" * 64,
                    "final_video_plan_sha256": "b" * 64,
                    "final_video_duration_seconds": 60.0,
                },
                output_dir=Path(self.tmp.name) / "output" / "forged-evidence",
                path=self.db,
            )

    def test_unbound_update_cannot_terminalize_running_job(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields(
                "job-1",
                {
                    "status": "completed",
                    "current_stage": "done",
                    "progress": 1.0,
                    "completed_at": "2026-01-01T00:00:00+00:00",
                },
                path=self.db,
            )

    def test_persisted_timestamp_and_terminal_stage_order_is_strict(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET created_at=?, updated_at=? WHERE id='job-1'",
                ("2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)

        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET created_at=?, updated_at=? WHERE id='job-1'",
                ("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
            )
            connection.execute(
                "UPDATE job_stages SET status='succeeded', started_at=?, completed_at=? WHERE job_id='job-1' AND stage_name='research'",
                ("2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)

    def test_terminal_stage_cannot_be_rewritten(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        job_store.update_stage(
            "job-1", "research", "succeeded", worker_id="worker-a", run_id=claim.job["run_id"],
            progress=0.2, artifact={"source": "first"}, path=self.db,
        )
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_stage(
                "job-1", "research", "succeeded", worker_id="worker-a", run_id=claim.job["run_id"],
                progress=0.2, artifact={"source": "rewrite"}, path=self.db,
            )

    def test_terminal_job_rejects_missing_configured_stage_row(self):
        job_store.create_job(
            "terminal-job",
            {"id": "terminal-job", "topic": "terminal"},
            output_dir=Path(self.tmp.name) / "output" / "terminal-job",
            path=self.db,
        )
        timestamp = job_store._now_iso()
        evidence = {
            "id": "terminal-job",
            "topic": "terminal",
            "package_status": "final_video_ready",
            "final_video_sha256": "a" * 64,
            "final_video_plan_sha256": "b" * 64,
            "final_video_duration_seconds": 60.0,
        }
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET input_json=?, status='completed', current_stage='done', progress=1.0, completed_at=?, updated_at=? WHERE id='terminal-job'",
                (json.dumps(evidence), timestamp, timestamp),
            )
            connection.execute(
                "UPDATE job_stages SET status='succeeded', attempt=1, started_at=?, completed_at=? WHERE job_id='terminal-job'",
                (timestamp, timestamp),
            )
            connection.commit()
        restored = job_store.get_job("terminal-job", path=self.db)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored["status"], "completed")
        with job_store.connect(self.db) as connection:
            connection.execute(
                "DELETE FROM job_stages WHERE job_id='terminal-job' AND stage_name='assembly'"
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("terminal-job", path=self.db)

    def test_queue_snapshot_repairs_missing_stage_graph_row(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "DELETE FROM job_stages WHERE job_id='job-1' AND stage_name='assembly'"
            )
            connection.commit()
        snapshot = job_store.queue_snapshot(path=self.db)
        self.assertEqual(snapshot["jobs"]["queued"], 1)
        with job_store.connect(self.db) as connection:
            restored = connection.execute(
                "SELECT status FROM job_stages WHERE job_id='job-1' AND stage_name='assembly'"
            ).fetchone()
        self.assertEqual(restored["status"], "pending")

    def test_claim_repairs_missing_stage_graph_row_before_persisting_lease(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "DELETE FROM job_stages WHERE job_id='job-1' AND stage_name='assembly'"
            )
            connection.commit()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(claim.job["status"], "running")
        with job_store.connect(self.db) as connection:
            row = connection.execute(
                "SELECT status, lease_owner, lease_expires_at FROM jobs WHERE id='job-1'"
            ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["lease_owner"], "worker-a")
        self.assertIsNotNone(row["lease_expires_at"])

    def test_active_job_cannot_use_done_as_current_stage(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET current_stage='done' WHERE id='job-1'"
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)

    def test_active_job_rejects_terminal_stage_after_incomplete_stage(self):
        self.create()
        timestamp = job_store._now_iso()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE job_stages SET status='succeeded', attempt=1, started_at=?, completed_at=? "
                "WHERE job_id='job-1' AND stage_name='script'",
                (timestamp, timestamp),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)

    def test_persisted_stage_timestamps_cannot_be_in_the_future(self):
        self.create()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE job_stages SET status='running', attempt=1, started_at=? "
                "WHERE job_id='job-1' AND stage_name='research'",
                (future,),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)

    def test_persisted_stage_timestamps_cannot_precede_job_creation(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE job_stages SET status='running', attempt=1, started_at=? "
                "WHERE job_id='job-1' AND stage_name='research'",
                ("2000-01-01T00:00:00+00:00",),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)

    def test_migration_normalizes_legacy_waiting_stage_for_active_job(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute("UPDATE jobs SET current_stage='waiting' WHERE id='job-1'")
            connection.commit()
        job_store.initialize(self.db)
        migrated = job_store.get_job("job-1", path=self.db)
        self.assertIsNotNone(migrated)
        assert migrated is not None
        self.assertEqual(migrated["status"], "queued")
        self.assertEqual(migrated["stage"], job_store.DEFAULT_STAGE_NAMES[0])

    def test_persisted_stage_execution_is_sequential(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        first_started = "2026-01-01T00:00:01+00:00"
        second_started = "2026-01-01T00:00:02+00:00"
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE job_stages SET status='running', attempt=1, started_at=? "
                "WHERE job_id='job-1' AND stage_name='research'",
                (first_started,),
            )
            connection.execute(
                "UPDATE job_stages SET status='running', attempt=1, started_at=? "
                "WHERE job_id='job-1' AND stage_name='script'",
                (second_started,),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)

    def test_progress_cannot_regress_within_an_attempt(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        assert claim is not None
        job_store.update_fields(
            "job-1", {"progress": 0.5}, worker_id="worker-a", run_id=claim.job["run_id"], path=self.db,
        )
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields(
                "job-1", {"progress": 0.4}, worker_id="worker-a", run_id=claim.job["run_id"], path=self.db,
            )

    def test_terminal_timestamp_cannot_precede_job_creation(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET status='failed', completed_at='2000-01-01T00:00:00+00:00' WHERE id='job-1'"
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)

    def test_persisted_duplicate_json_keys_fail_closed(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET input_json=? WHERE id='job-1'",
                ('{"topic":"safe","topic":{}}',),
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)

    def test_unbound_worker_cannot_fail_running_job(self):
        self.create()
        claim = job_store.claim_next_job("worker-a", path=self.db)
        self.assertIsNotNone(claim)
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.update_fields(
                "job-1", {"status": "failed", "error": "forged"}, path=self.db,
            )
        current = job_store.get_job("job-1", path=self.db)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["status"], "running")

    def test_future_terminal_timestamp_fails_closed(self):
        self.create()
        with job_store.connect(self.db) as connection:
            connection.execute(
                "UPDATE jobs SET status='failed', completed_at='2999-01-01T00:00:00+00:00' WHERE id='job-1'"
            )
            connection.commit()
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.get_job("job-1", path=self.db)

    def test_legacy_completed_requires_actual_final_video(self):
        source = Path(self.tmp.name) / "jobs.json"
        source.write_text(json.dumps({
            "legacy": {
                "id": "legacy",
                "status": "completed",
                "package_status": "final_video_ready",
                "final_video_sha256": "a" * 64,
                "final_video_plan_sha256": "b" * 64,
                "final_video_duration_seconds": 60.0,
                "output_dir": str(Path(self.tmp.name) / "output" / "legacy"),
            },
        }))
        with self.assertRaises(job_store.InvalidStoreState):
            job_store.import_jobs_json_once(source, path=self.db)
        self.assertEqual(job_store.list_jobs(path=self.db), [])


if __name__ == "__main__":
    unittest.main()
