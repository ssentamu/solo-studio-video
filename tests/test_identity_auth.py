import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import api
import job_store


class IdentityFileAuthTests(unittest.TestCase):
    def test_identity_file_auth_issues_owner_scoped_durable_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identities = root / "identities.json"
            identities.write_text(json.dumps({"alice": "alice-secret"}))
            original = {name: getattr(api, name) for name in (
                "API_TOKEN", "TOKEN_IDENTITIES_FILE", "DATABASE_CONFIGURED", "AUTH_DB_ENABLED",
                "DATABASE_FILE", "OUTPUT_ROOT", "COOKIE_SECURE", "REQUIRE_API_TOKEN",
            )}
            try:
                api.API_TOKEN = ""
                api.TOKEN_IDENTITIES_FILE = identities
                api.DATABASE_CONFIGURED = True
                api.AUTH_DB_ENABLED = True
                api.DATABASE_FILE = root / "state.sqlite3"
                api.OUTPUT_ROOT = root / "output"
                api.OUTPUT_ROOT.mkdir()
                api.COOKIE_SECURE = False
                api.REQUIRE_API_TOKEN = True
                job_store.initialize(api.DATABASE_FILE)
                with TestClient(api.app) as client:
                    login = client.post("/api/auth/session", json={"token": "alice-secret"}, headers={"Origin": "https://edgescout.tech"})
                    self.assertEqual(login.status_code, 204)
                    created = client.post("/api/jobs", json={"topic": "owner scoped", "duration_minutes": 1}, headers={"Origin": "https://edgescout.tech"})
                self.assertEqual(created.status_code, 201)
                jobs = job_store.list_jobs(path=api.DATABASE_FILE)
                self.assertEqual(jobs[0]["owner_id"], "alice")
            finally:
                for name, value in original.items():
                    setattr(api, name, value)


if __name__ == "__main__":
    unittest.main()
