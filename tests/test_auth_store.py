import tempfile
import unittest
from pathlib import Path

import auth_store


class AuthStoreTests(unittest.TestCase):
    def test_session_is_hashed_durable_and_revocable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            token = auth_store.create_session(db, "user-1", 300)
            session = auth_store.validate_session(db, token)
            self.assertIsNotNone(session)
            self.assertEqual((session or {})["owner_id"], "user-1")
            auth_store.revoke_session(db, token)
            self.assertIsNone(auth_store.validate_session(db, token))
            with auth_store.job_store.connect(db) as connection:
                row = connection.execute("SELECT token_digest FROM auth_sessions").fetchone()
            self.assertNotEqual(row["token_digest"], token)

    def test_audit_event_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            auth_store.audit(db, "user-1", "job.created", job_id="abc", metadata={"source": "api"})
            with auth_store.job_store.connect(db) as connection:
                row = connection.execute("SELECT owner_id, action, job_id FROM audit_events").fetchone()
            self.assertEqual(dict(row), {"owner_id": "user-1", "action": "job.created", "job_id": "abc"})


if __name__ == "__main__":
    unittest.main()
