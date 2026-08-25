import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import job_store


ROOT = Path(__file__).resolve().parents[1]

# The exact record shapes observed in production state/jobs.json: a failed job
# stopped at the final "assembly" stage and a completed job with the historical
# flat-store "done" stage, both with stage_names serialized as null.
PRODUCTION_LEGACY_JOBS = {
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
    "e80162e71314": {
        "id": "e80162e71314",
        "topic": "ok",
        "target_audience": "general",
        "duration_seconds": 60,
        "platform": "youtube",
        "tone": "professional",
        "key_messages": [],
        "visual_style": "",
        "call_to_action": "",
        "status": "completed",
        "progress": 1.0,
        "stage": "done",
        "format": "",
        "chapters": 0,
        "scenes": 0,
        "created_at": "2026-08-18T15:41:36.736704+00:00",
        "completed_at": None,
        "error": None,
        "has_visuals": True,
        "has_voiceover": True,
        "has_clips": True,
        "has_final_video": True,
        "package_status": "completed",
        "stage_names": None,
    },
}


class RuntimeInitStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.database = self.state / "solo_studio.sqlite3"
        self.jobs_file = self.state / "jobs.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_runtime_init(self):
        env = dict(
            os.environ,
            SOLO_STUDIO_DATABASE_FILE=str(self.database),
            SOLO_STUDIO_JOBS_FILE=str(self.jobs_file),
        )
        return subprocess.run(
            [sys.executable, str(ROOT / "runtime_init.py")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_startup_imports_production_legacy_records_and_reboots_cleanly(self):
        self.jobs_file.write_text(json.dumps(PRODUCTION_LEGACY_JOBS))

        first_boot = self.run_runtime_init()
        self.assertEqual(first_boot.returncode, 0, first_boot.stderr)

        jobs = job_store.list_jobs(path=self.database)
        self.assertEqual(len(jobs), 2)
        failed = job_store.get_job("68539c757ce7", self.database)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["stage"], "assembly")
        self.assertEqual(failed["progress"], 0.92)
        self.assertEqual(failed["error"], "name 'timezone' is not defined")
        completed = job_store.get_job("e80162e71314", self.database)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["stage"], "done")
        self.assertEqual(completed["completed_at"], "2026-08-18T15:41:36.736704+00:00")

        second_boot = self.run_runtime_init()
        self.assertEqual(second_boot.returncode, 0, second_boot.stderr)
        self.assertEqual(len(job_store.list_jobs(path=self.database)), 2)

    def test_startup_still_fails_closed_on_malformed_legacy_state(self):
        self.jobs_file.write_text('{"bad": {"id": "mismatched"}}')

        result = self.run_runtime_init()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("InvalidStoreState", result.stderr)
        self.assertEqual(len(job_store.list_jobs(path=self.database)), 0)

    def test_startup_rejects_symlinked_legacy_jobs_file(self):
        real = Path(self.tmp.name) / "real-jobs.json"
        real.write_text("{}")
        self.jobs_file.symlink_to(real)

        result = self.run_runtime_init()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)


if __name__ == "__main__":
    unittest.main()
