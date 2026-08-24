import fnmatch
import os
import re
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
                "package_utils.py",
                "engines/",
                "pipeline.py",
                "frontend/",
                "templates.json",
            ],
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertFalse(self.dockerignore_excludes(source))

    def test_deployment_removals_are_verified_and_unknown_inspect_fails_closed(self):
        self.assertEqual(self.deploy.count('docker rm -f'), 1)
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

    def test_replacement_attempt_is_reconciled_before_rollback_mutation(self):
        self.assertIn("replacement_started=1", self.deploy)
        self.assertIn("reconcile_replacement", self.deploy)
        self.assertIn("start_container_and_reconcile", self.deploy)
        self.assertIn("Replacement container existence could not be reconciled", self.deploy)
        self.assertIn("refusing rollback mutation", self.deploy)
        self.assertNotIn('if docker inspect "$APP_NAME" >/dev/null 2>&1; then', self.deploy)
        rollback_start = self.deploy.index("rollback_live()")
        rollback_end = self.deploy.index("rollback_signal()")
        rollback = self.deploy[rollback_start:rollback_end]
        self.assertLess(
            rollback.index("remove_container_and_verify \"$APP_NAME\""),
            rollback.index("start_container_and_reconcile \"$rollback_tag\""),
        )

    def run_preflight_fake_docker(self, mode):
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

    def test_preflight_ambiguous_removal_preserves_mount_directories(self):
        completed, events, output = self.run_preflight_fake_docker("ambiguous")

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("RESULT=1", output)
        self.assertIn("EXEC", events)
        self.assertIn("RM", events)
        self.assertFalse(any(event.startswith("CLEANUP") for event in events), events)
        state_line = next(line for line in output.splitlines() if line.startswith("STATE="))
        output_line = next(line for line in output.splitlines() if line.startswith("OUTPUT="))
        self.assertTrue(Path(state_line.removeprefix("STATE=")).is_dir(), output)
        self.assertTrue(Path(output_line.removeprefix("OUTPUT=")).is_dir(), output)


if __name__ == "__main__":
    unittest.main()
