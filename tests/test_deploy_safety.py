import fnmatch
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.deploy = (ROOT / "deploy-traefik.sh").read_text()
        cls.dockerignore = (ROOT / ".dockerignore").read_text()
        cls.ignore_patterns = [
            line.strip()
            for line in cls.dockerignore.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def dockerignore_excludes(self, path: str) -> bool:
        """Check the positive patterns used here for representative context paths."""
        path = path.lstrip("/")
        basename = Path(path).name
        for pattern in self.ignore_patterns:
            if pattern.startswith("!"):
                continue
            directory_pattern = pattern.endswith("/")
            normalized = pattern.rstrip("/")
            if directory_pattern and (
                path == normalized
                or path.startswith(normalized + "/")
                or fnmatch.fnmatch(path, normalized + "/**")
            ):
                return True
            if not directory_pattern and (
                fnmatch.fnmatch(path, normalized)
                or ("/" not in normalized and fnmatch.fnmatch(basename, normalized))
            ):
                return True
        return False

    def test_dockerignore_excludes_runtime_secrets_state_and_tooling(self):
        excluded_paths = (
            "state/solo_studio.sqlite3",
            "state/solo_studio.sqlite3-wal",
            "state/solo_studio.sqlite3-shm",
            "state/solo_studio.sqlite3-journal",
            "var/log/app.log",
            "nested/.cache/model.bin",
            ".venv/bin/python",
            "frontend/node_modules/package/index.js",
            "subdir/.npmrc",
            "subdir/.netrc",
            ".github/workflows/ci.yml",
        )
        for path in excluded_paths:
            with self.subTest(path=path):
                self.assertTrue(self.dockerignore_excludes(path))

    def test_dockerignore_keeps_every_dockerfile_copy_source(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        sources = re.findall(r"^COPY\s+(\S+)(?:\s+\S+)?$", dockerfile, flags=re.MULTILINE)
        self.assertEqual(
            sources,
            [
                "requirements.txt",
                "api.py",
                "worker.py",
                "runtime_init.py",
                "job_store.py",
                "auth_store.py",
                "media_assembly.py",
                "audio_generation.py",
                "music_generation.py",
                "package_utils.py",
                "provider_canary.py",
                "engines/",
                "pipeline.py",
                "frontend/",
                "templates.json",
            ],
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertFalse(self.dockerignore_excludes(source))

    def test_authenticated_preflight_builds_curl_config_from_token_file(self):
        self.assertNotIn("SOLO_...KEN", self.deploy)
        self.assertIn('os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)', self.deploy)
        self.assertIn('os.fstat(token_fd)', self.deploy)
        self.assertIn('os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)', self.deploy)
        self.assertIn('"$SOLO_STUDIO_API_TOKEN_FILE" "$curl_config"', self.deploy)
        self.assertIn('/run/secrets/solo_studio_api_token', self.deploy)
        self.assertIn('--config "$token_config"', self.deploy)
        forbidden_token_expansion = "Authorization: Bearer $" + "SOLO_STUDIO_API_TOKEN"
        self.assertNotIn(forbidden_token_expansion, self.deploy)

    def test_ephemeral_token_is_cleaned_on_early_validation_failure(self):
        fallback = self.deploy.index('if [ ! -e "$SOLO_STUDIO_API_TOKEN_FILE" ]')
        early_trap = self.deploy.index("trap cleanup_ephemeral_api_token EXIT")
        self.assertLess(early_trap, fallback)
        self.assertIn('rm -f -- "$SOLO_STUDIO_API_TOKEN_FILE"', self.deploy)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = os.environ.copy()
            env.update({
                "TMPDIR": str(root),
                "HOME": str(root),
                "SOLO_STUDIO_API_TOKEN": "synthetic-token",
                "SOLO_STUDIO_API_TOKEN_FILE": str(root / "missing-token"),
                "SOLO_STUDIO_ENABLE_HIGGSFIELD": "invalid",
            })
            result = subprocess.run(
                ["bash", str(ROOT / "deploy-traefik.sh")],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(root.iterdir()), [])

    def test_ephemeral_token_partial_mktemp_failure_is_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_mktemp = fake_bin / "mktemp"
            fake_mktemp.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "template=${1:?missing template}\n"
                "partial=${template%XXXXXX}partial\n"
                "(umask 022; : > \"$partial\")\n"
                "exit 17\n"
            )
            fake_mktemp.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "PATH": f"{fake_bin}:{env['PATH']}",
                "TMPDIR": str(root),
                "HOME": str(root),
                "SOLO_STUDIO_API_TOKEN": "synthetic-token",
                "SOLO_STUDIO_API_TOKEN_FILE": str(root / "missing-token"),
            })
            result = subprocess.run(
                ["bash", str(ROOT / "deploy-traefik.sh")],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 17)
            self.assertEqual(list(root.iterdir()), [fake_bin])
            self.assertEqual(list(fake_bin.iterdir()), [fake_mktemp])

    def test_deployment_removals_are_verified_and_unknown_inspect_fails_closed(self):
        self.assertEqual(self.deploy.count('docker rm -f'), 1)
        self.assertIn('docker container inspect "$container_name"', self.deploy)
        self.assertIn("docker container inspect -f '{{.Image}}'", self.deploy)
        self.assertIn('grep -Eiq "No such (object|container)"', self.deploy)
        self.assertIn("return 2", self.deploy)
        remove_start = self.deploy.index("remove_container_and_verify()")
        remove_end = self.deploy.index("reconcile_replacement()")
        remove_helper = self.deploy[remove_start:remove_end]
        self.assertLess(remove_helper.index('docker rm -f'), remove_helper.index("container_inspect_state"))
        self.assertIn("Refusing to continue after removing", remove_helper)
        self.assertIn("remove_container_and_verify \"$preflight_name\"", self.deploy)
        self.assertIn("PREFLIGHT_DIRS_SAFE_TO_DELETE=0", self.deploy)
        self.assertIn("clear_preflight_dirs", self.deploy)
        self.assertIn("remove_container_and_verify \"$APP_NAME\"", self.deploy)

    def test_higgsfield_default_resolution_matches_verified_media_contract(self):
        self.assertIn("SOLO_STUDIO_HIGGSFIELD_RESOLUTION=${SOLO_STUDIO_HIGGSFIELD_RESOLUTION:-1080p}", self.deploy)

    def test_replacement_attempt_is_reconciled_before_rollback_mutation(self):
        self.assertIn("replacement_started=1", self.deploy)
        self.assertIn("reconcile_replacement", self.deploy)
        self.assertIn("start_container_and_reconcile", self.deploy)
        self.assertIn("Replacement container existence could not be reconciled", self.deploy)
        self.assertIn("refusing rollback mutation", self.deploy)
        self.assertNotIn('if docker inspect "$APP_NAME" >/dev/null 2>&1; then', self.deploy)
        self.assertIn("docker container inspect -f '{{.State.Status}}' \"$APP_NAME\"", self.deploy)
        self.assertIn("restarting|exited|created|dead|paused)", self.deploy)
        rollback_start = self.deploy.index("rollback_live()")
        rollback_end = self.deploy.index("rollback_signal()")
        rollback = self.deploy[rollback_start:rollback_end]
        self.assertLess(
            rollback.index("remove_container_and_verify \"$APP_NAME\""),
            rollback.index("start_container_and_reconcile \"$rollback_tag\""),
        )

    def test_public_health_uses_bounded_retry_before_direct_public_request(self):
        local_smoke = self.deploy.index('echo "=== Local smoke test ==="')
        public_smoke = self.deploy.index('echo "=== Public Traefik smoke test ==="')
        local_block = self.deploy[local_smoke:public_smoke]
        public_block = self.deploy[public_smoke:]
        self.assertNotIn('curl "${CURL_BOUNDED[@]}" -fsS "$DOMAIN/api/health"', local_block)
        self.assertLess(
            public_block.index("wait_for_public_health"),
            public_block.index('curl "${CURL_BOUNDED[@]}" -fsS "$DOMAIN/api/health"'),
        )

    def run_preflight_fake_docker(
        self, mode, state_jobs=None, state_jobs_symlink=False, state_database=False
    ):
        """Run the real preflight function against a deterministic fake Docker."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            event_log = root / "events.log"
            container_state = root / "container.state"
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                f"log={event_log!s}\n"
                f"state={container_state!s}\n"
                f"mode={mode!s}\n"
                "command=${1:-}\n"
                "if [ \"$command\" = container ]; then command=${2:-}; fi\n"
                "case \"$command\" in\n"
                "  run)\n"
                "    printf '%s\\n' RUN >> \"$log\"\n"
                "    printf '%s\\n' present > \"$state\"\n"
                "    if [ \"$mode\" = startup-failure ]; then exit 17; fi\n"
                "    exit 0\n"
                "    ;;\n"
                "  exec)\n"
                "    printf '%s\\n' EXEC >> \"$log\"\n"
                "    exit 0\n"
                "    ;;\n"
                "  rm)\n"
                "    printf '%s\\n' RM >> \"$log\"\n"
                "    if [ \"$mode\" = ambiguous ] && [ -f \"$state\" ]; then\n"
                "      printf '%s\\n' ambiguous > \"$state\"\n"
                "    else\n"
                "      /bin/rm -f \"$state\"\n"
                "    fi\n"
                "    exit 0\n"
                "    ;;\n"
                "  inspect)\n"
                "    if [ -f \"$state\" ] && [ \"$(cat \"$state\")\" = present ]; then exit 0; fi\n"
                "    if [ -f \"$state\" ] && [ \"$(cat \"$state\")\" = ambiguous ]; then\n"
                "      printf '%s\\n' 'daemon unavailable' >&2\n"
                "      exit 2\n"
                "    fi\n"
                "    if [ \"$mode\" = no-such-container ]; then\n"
                "      printf '%s\\n' 'Error response from daemon: No such container: solo-studio-video-release-preflight' >&2\n"
                "    else\n"
                "      printf '%s\\n' 'No such object' >&2\n"
                "    fi\n"
                "    exit 1\n"
                "    ;;\n"
                "esac\n"
                "exit 99\n"
            )
            fake_docker.chmod(0o755)
            fake_rm = fake_bin / "rm"
            fake_rm.write_text(
                "#!/bin/sh\n"
                f"printf 'CLEANUP %s\\n' \"$*\" >> {event_log!s}\n"
                "exec /bin/rm \"$@\"\n"
            )
            fake_rm.chmod(0o755)

            app_dir_setup = ""
            if state_jobs is not None or state_jobs_symlink:
                app_dir = root / "appdir"
                (app_dir / "state").mkdir(parents=True)
                jobs_path = app_dir / "state" / "jobs.json"
                if state_jobs_symlink:
                    real_target = root / "real-jobs.json"
                    real_target.write_text("{}")
                    jobs_path.symlink_to(real_target)
                else:
                    jobs_path.write_text(state_jobs)
                app_dir_setup = f"APP_DIR={app_dir}\n"
            elif state_database:
                app_dir = root / "appdir"
                (app_dir / "state").mkdir(parents=True)
                database = sqlite3.connect(app_dir / "state" / "solo_studio.sqlite3")
                try:
                    database.execute("CREATE TABLE marker (value TEXT)")
                    database.execute("INSERT INTO marker VALUES ('real-db')")
                    database.commit()
                finally:
                    database.close()
                app_dir_setup = f"APP_DIR={app_dir}\n"

            start = self.deploy.index("#!/bin/bash")
            function_source = self.deploy[start:self.deploy.index("preflight_release_image() {")]
            harness = (
                function_source
                + "\n"
                + f"export PATH={fake_bin}:$PATH\n"
                + "export SOLO_STUDIO_API_TOKEN=test-token\n"
                + "runtime_uid=$(id -u)\n"
                + "runtime_gid=$(id -g)\n"
                + "CONTAINER_USER_ARGS=()\n"
                + app_dir_setup
                + "if preflight_image test-image release; then result=0; else result=$?; fi\n"
                + "printf 'RESULT=%s\\nSTATE=%s\\nOUTPUT=%s\\n' \"$result\" \"$PREFLIGHT_STATE_DIR\" \"$PREFLIGHT_OUTPUT_DIR\"\n"
            )
            harness_path = root / "harness.sh"
            harness_path.write_text(harness)
            harness_path.chmod(0o755)
            completed = subprocess.run(
                ["bash", str(harness_path)],
                cwd=root,
                env={**os.environ, "HOME": str(root), "SOLO_STUDIO_API_TOKEN": "test-token"},
                text=True,
                capture_output=True,
                timeout=30,
            )
            events = event_log.read_text().splitlines() if event_log.exists() else []
            output = completed.stdout + completed.stderr
            return completed, events, output

    def test_preflight_executes_checks_before_removal_and_mount_cleanup(self):
        completed, events, output = self.run_preflight_fake_docker("success")

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("RESULT=0", output)
        self.assertLess(events.index("EXEC"), events.index("RM", events.index("RUN")))
        cleanup_events = [index for index, event in enumerate(events) if event.startswith("CLEANUP")]
        self.assertTrue(cleanup_events, events)
        self.assertLess(events.index("EXEC"), cleanup_events[0])

    def test_preflight_startup_failure_reconciles_before_cleanup_without_exec(self):
        completed, events, output = self.run_preflight_fake_docker("startup-failure")

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("RESULT=1", output)
        self.assertNotIn("EXEC", events)
        run_index = events.index("RUN")
        remove_index = events.index("RM", run_index)
        cleanup_index = next(index for index, event in enumerate(events) if event.startswith("CLEANUP"))
        self.assertLess(run_index, remove_index)
        self.assertLess(remove_index, cleanup_index)

    def test_preflight_accepts_docker_no_such_container_message(self):
        completed, events, output = self.run_preflight_fake_docker("no-such-container")

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("RESULT=0", output)
        self.assertLess(events.index("EXEC"), events.index("RM", events.index("RUN")))

    def test_preflight_state_seed_copies_real_legacy_jobs_file(self):
        sentinel = '{"job-1": {"id": "job-1"}}'
        completed, events, output = self.run_preflight_fake_docker(
            "ambiguous", state_jobs=sentinel
        )

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("RESULT=1", output)
        self.assertIn("RUN", events)
        state_line = next(line for line in output.splitlines() if line.startswith("STATE="))
        seeded = Path(state_line.removeprefix("STATE=")) / "jobs.json"
        self.assertEqual(seeded.read_text(), sentinel)

    def test_preflight_refuses_symlinked_state_jobs_seed(self):
        completed, events, output = self.run_preflight_fake_docker(
            "success", state_jobs_symlink=True
        )

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("RESULT=1", output)
        self.assertNotIn("RUN", events)
        self.assertIn("Refusing symlinked or non-regular state/jobs.json", output)

    def test_preflight_seed_copy_is_nofollow_and_exclusive(self):
        self.assertIn(
            'copy_regular_file_excl "$APP_DIR/state/jobs.json" "$PREFLIGHT_STATE_DIR/jobs.json" 600',
            self.deploy,
        )
        self.assertIn("O_NOFOLLOW", self.deploy)
        self.assertIn("O_NONBLOCK", self.deploy)
        self.assertIn("os.link(temp_path, sys.argv[2], follow_symlinks=False)", self.deploy)
        self.assertIn("stat.S_ISREG(os.fstat(source_fd).st_mode)", self.deploy)
        self.assertIn("copy_sqlite_database_excl", self.deploy)

    def test_preflight_state_seed_copies_real_sqlite_database(self):
        completed, events, output = self.run_preflight_fake_docker(
            "ambiguous", state_database=True
        )

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("RESULT=1", output)
        state_line = next(line for line in output.splitlines() if line.startswith("STATE="))
        seeded = Path(state_line.removeprefix("STATE=")) / "solo_studio.sqlite3"
        database = sqlite3.connect(seeded)
        try:
            self.assertEqual(database.execute("SELECT value FROM marker").fetchone(), ("real-db",))
        finally:
            database.close()

    def test_preflight_rejects_fifo_state_jobs_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_path = root / "jobs.json"
            os.mkfifo(jobs_path)
            source = self.deploy.index("#!/bin/bash")
            helper = self.deploy[source:self.deploy.index("container_inspect_state()")]
            harness = (
                helper
                + f"copy_regular_file_excl {jobs_path} {root / 'copy.json'} 600"
            )
            completed = subprocess.run(
                ["bash", "-c", harness],
                cwd=root,
                text=True,
                capture_output=True,
                timeout=5,
            )
            self.assertNotEqual(completed.returncode, 0)

    def test_preflight_ambiguous_removal_preserves_mount_directories(self):
        completed, events, output = self.run_preflight_fake_docker("ambiguous")

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("RESULT=1", output)
        self.assertIn("EXEC", events)
        self.assertIn("RM", events)
        state_line = next(line for line in output.splitlines() if line.startswith("STATE="))
        output_line = next(line for line in output.splitlines() if line.startswith("OUTPUT="))
        state_path = state_line.removeprefix("STATE=")
        output_path = output_line.removeprefix("OUTPUT=")
        cleanup_events = [event for event in events if event.startswith("CLEANUP")]
        self.assertTrue(cleanup_events, events)
        self.assertTrue(all(state_path not in event and output_path not in event for event in cleanup_events), events)
        self.assertTrue(Path(state_path).is_dir(), output)
        self.assertTrue(Path(output_path).is_dir(), output)


if __name__ == "__main__":
    unittest.main()
