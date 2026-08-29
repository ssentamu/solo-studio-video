import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import api
import job_store
import package_utils


class IdentityFileAuthTests(unittest.TestCase):
    def test_api_uses_shared_secure_text_reader(self):
        self.assertIs(api._read_secure_text_file, package_utils._read_secure_text_file)

    def test_required_auth_does_not_fail_open_when_token_is_unavailable(self):
        original = {
            name: getattr(api, name)
            for name in ("API_TOKEN", "TOKEN_IDENTITIES_FILE", "REQUIRE_API_TOKEN", "API_TOKEN_FILE")
        }
        try:
            api.API_TOKEN = ""
            api.TOKEN_IDENTITIES_FILE = None
            api.REQUIRE_API_TOKEN = True
            api.API_TOKEN_FILE = Path("/nonexistent/hermes-token")
            with TestClient(api.app) as client:
                response = client.get("/api/jobs")
            self.assertEqual(response.status_code, 401)
        finally:
            for name, value in original.items():
                setattr(api, name, value)

    def test_identity_file_auth_issues_owner_scoped_durable_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identities = root / "identities.json"
            identities.write_text(json.dumps({"alice": "alice-secret"}))
            identities.chmod(0o600)
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

    def test_identity_file_fifo_does_not_block_token_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "identities.fifo"
            os.mkfifo(fifo)
            original = api.TOKEN_IDENTITIES_FILE
            try:
                api.TOKEN_IDENTITIES_FILE = fifo
                self.assertIsNone(api._token_owner("synthetic-token"))
            finally:
                api.TOKEN_IDENTITIES_FILE = original

    def test_identity_file_rejects_control_character_token(self):
        with tempfile.TemporaryDirectory() as directory:
            identities = Path(directory) / "identities.json"
            identities.write_text(json.dumps({"alice": "synthetic\u0000token"}))
            identities.chmod(0o600)
            original = api.TOKEN_IDENTITIES_FILE
            try:
                api.TOKEN_IDENTITIES_FILE = identities
                self.assertIsNone(api._token_owner("synthetic\u0000token"))
            finally:
                api.TOKEN_IDENTITIES_FILE = original

    def test_environment_token_rejects_control_characters(self):
        original_file = api.API_TOKEN_FILE
        original_value = os.environ.get("SOLO_STUDIO_API_TOKEN")
        try:
            api.API_TOKEN_FILE = None
            for value in ("\nsynthetic-token", "synthetic-token\n", "synthetic\ttoken", "synthetic\x7ftoken"):
                with self.subTest(control=repr(value)):
                    os.environ["SOLO_STUDIO_API_TOKEN"] = value
                    self.assertEqual(api._load_api_token(), "")
        finally:
            api.API_TOKEN_FILE = original_file
            if original_value is None:
                os.environ.pop("SOLO_STUDIO_API_TOKEN", None)
            else:
                os.environ["SOLO_STUDIO_API_TOKEN"] = original_value

    def test_primary_token_rejects_symlinked_parent_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_parent = root / "real"
            real_parent.mkdir()
            token_file = real_parent / "token"
            token_file.write_text("synthetic-token")
            token_file.chmod(0o600)
            link_parent = root / "alias"
            link_parent.symlink_to(real_parent, target_is_directory=True)
            original_file = api.API_TOKEN_FILE
            try:
                api.API_TOKEN_FILE = link_parent / "token"
                self.assertEqual(api._load_api_token(), "")
            finally:
                api.API_TOKEN_FILE = original_file

    def test_token_validation_rejects_curl_unsafe_characters(self):
        for value in ('synthetic"token', "synthetic\\token"):
            with self.subTest(value=repr(value)):
                self.assertFalse(api._valid_token_value(value))


if __name__ == "__main__":
    unittest.main()
