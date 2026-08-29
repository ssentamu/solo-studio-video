import hashlib
import json
import io
import os
import threading
import re
import stat
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from typing import Any, cast
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import package_utils

from package_utils import _generation_plan_info, _probe_media, _public_summary, atomic_write_json, clear_generated_artifacts, compute_package_status, normalize_output_profile, read_json_object, update_json_file, validate_output_profile_contract, write_package_manifest
from engines.generation_agent import generate_plan, run_higgsfield
import job_store
import worker


class PackageStatusTests(unittest.TestCase):
    def test_claim_identity_rejects_same_inode_ctime_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            victim = root / "victim"
            victim.write_text("safe", encoding="utf-8")
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                before = victim.stat()
                expected = package_utils._entry_cleanup_identity_at(parent_fd, victim.name)
                time.sleep(0.002)
                os.utime(victim, ns=(before.st_atime_ns, before.st_mtime_ns))
                self.assertNotEqual(victim.stat().st_ctime_ns, before.st_ctime_ns)
                with self.assertRaises(OSError):
                    package_utils._remove_entry_at(parent_fd, victim.name, expected)
                self.assertTrue(victim.exists())
            finally:
                os.close(parent_fd)

    def test_remove_entry_recovers_mutation_during_destructive_unlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            victim = root / "victim"
            victim.write_text("safe", encoding="utf-8")
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            real_unlink = package_utils.os.unlink
            try:
                expected = package_utils._entry_cleanup_identity_at(parent_fd, victim.name)

                def mutate_before_unlink(name, *args, **kwargs):
                    if name == "entry":
                        fd = os.open(name, os.O_WRONLY | os.O_APPEND, dir_fd=kwargs["dir_fd"])
                        try:
                            os.write(fd, b"changed")
                        finally:
                            os.close(fd)
                    return real_unlink(name, *args, **kwargs)

                with patch.object(package_utils.os, "unlink", side_effect=mutate_before_unlink):
                    with self.assertRaises(OSError):
                        package_utils._remove_entry_at(parent_fd, victim.name, expected)
                recovered = list(root.glob(".recovered-*"))
                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].read_text(encoding="utf-8"), "safechanged")
            finally:
                os.close(parent_fd)

    def test_private_staging_fstat_failure_closes_and_removes_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch.object(package_utils.os, "fstat", side_effect=OSError("injected fstat failure")):
                    with self.assertRaises(OSError):
                        package_utils._open_private_staging_directory(parent_fd, prefix=".probe-")
                self.assertEqual(list(root.glob(".probe-*")), [])
            finally:
                os.close(parent_fd)

    def test_private_staging_post_mkdir_stat_failure_closes_and_removes_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            real_stat = package_utils.os.stat
            try:
                def fail_post_mkdir_stat(name, *args, **kwargs):
                    if isinstance(name, str) and name.startswith(".probe-"):
                        raise OSError("injected post-mkdir stat failure")
                    return real_stat(name, *args, **kwargs)

                with patch.object(package_utils.os, "stat", side_effect=fail_post_mkdir_stat):
                    with self.assertRaises(OSError):
                        package_utils._open_private_staging_directory(parent_fd, prefix=".probe-")
                self.assertEqual(list(root.glob(".probe-*")), [])
            finally:
                os.close(parent_fd)

    def test_containment_exchange_failure_does_not_rename_unverified_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical"
            trusted = root / "trusted"
            canonical.write_text("replacement", encoding="utf-8")
            trusted.write_text("trusted", encoding="utf-8")
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                expected = package_utils._entry_cleanup_identity_at(parent_fd, trusted.name)
                with patch("package_utils._rename_exchange", side_effect=OSError("exchange unavailable")):
                    with self.assertRaises(OSError):
                        package_utils._contain_entry_at(parent_fd, canonical.name, expected, "probe")
                self.assertTrue(canonical.exists())
                self.assertEqual(canonical.read_text(encoding="utf-8"), "replacement")
            finally:
                os.close(parent_fd)

    def test_private_staging_open_failure_removes_created_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                real_open = package_utils.os.open

                def fail_staging_open(name, *args, **kwargs):
                    if isinstance(name, str) and name.startswith(".probe-"):
                        raise OSError("injected staging open failure")
                    return real_open(name, *args, **kwargs)

                with patch.object(package_utils.os, "open", side_effect=fail_staging_open):
                    with self.assertRaises(OSError):
                        package_utils._open_private_staging_directory(parent_fd, prefix=".probe-")
                self.assertEqual(list(root.glob(".probe-*")), [])
            finally:
                os.close(parent_fd)

    def test_remove_entry_at_can_require_held_directory_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / ".held"
            staged.mkdir()
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            staged_fd = os.open(staged, os.O_RDONLY | os.O_DIRECTORY)
            try:
                staged_stat = os.fstat(staged_fd)
                package_utils._remove_entry_at(
                    parent_fd,
                    staged.name,
                    (staged_stat.st_dev, staged_stat.st_ino, "held-descriptor"),
                    held_fd=staged_fd,
                )
                self.assertFalse(staged.exists())
            finally:
                os.close(staged_fd)
                os.close(parent_fd)

    def test_public_summary_whitelists_probe_metadata(self):
        summary = _public_summary(
            {"final_video_probe": {
                "path": "/tmp/output/final.mp4",
                "size_bytes": 123,
                "sha256": "a" * 64,
                "duration_seconds": 2.0,
                "format_name": "mp4",
                "has_audio": True,
                "has_video": True,
                "width": 1920,
                "height": 1080,
                "streams": [{"codec_type": "video", "tags": {"comment": "SECRET"}}],
                "format": {"tags": {"comment": "SECRET"}},
                "tags": {"comment": "SECRET"},
            }},
            Path("/tmp/output"),
        )
        probe = summary["final_video_probe"]
        self.assertEqual(probe["path"], "final.mp4")
        self.assertNotIn("streams", probe)
        self.assertNotIn("format", probe)
        self.assertNotIn("tags", probe)

    def test_strict_json_rejects_excessive_nesting(self):
        from package_utils import _parse_strict_json

        with self.assertRaises(ValueError):
            _parse_strict_json("[" * 300 + "0" + "]" * 300)

    def test_safe_error_redacts_aws_identifiers_and_mixed_case_urls(self):
        rendered = worker._safe_error("AKIAABCDEFGHIJKLMNOP HTTPS://EXAMPLE.COM/signed?token=secret")
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", rendered)
        self.assertIn("[credential-redacted]", rendered)
        mixed_case_aws = worker._safe_error("aKiA1234567890123456")
        self.assertNotIn("aKiA1234567890123456", mixed_case_aws)
        self.assertIn("[credential-redacted]", mixed_case_aws)
        self.assertNotIn("HTTPS://EXAMPLE.COM", rendered)
        self.assertIn("[provider-url-redacted]", rendered)

        token_key = "refresh_" + "token"
        redacted = worker._safe_error(f"topic\n\x1b[31m{token_key}: secret-value\x1b[0m")
        self.assertNotIn("\n", redacted)
        self.assertNotIn("\x1b", redacted)
        self.assertNotIn("secret-value", redacted)
        self.assertIn("[redacted]", redacted)
        bearer_url = worker._safe_error("Bearer https://provider.invalid/path?token=secret-value")
        self.assertNotIn("provider.invalid", bearer_url)
        self.assertNotIn("secret-value", bearer_url)
        quoted_json = worker._safe_error('{"token":"quoted-secret"}')
        self.assertNotIn("quoted-secret", quoted_json)
        quoted_whitespace = worker._safe_error('{"token":"secret value with spaces"}')
        self.assertNotIn("secret value with spaces", quoted_whitespace)
        self.assertIn("[redacted]", quoted_whitespace)
        escaped_secret = r'escaped \"value\" tail'
        for syntax in (
            'token: "secret value with spaces"',
            'token = "secret value with spaces"',
            'password: "' + escaped_secret + '"',
        ):
            rendered = worker._safe_error(syntax)
            self.assertNotIn("secret value with spaces", rendered)
            self.assertNotIn(escaped_secret, rendered)
            self.assertIn("[redacted]", rendered)

    def test_thumbnail_prompt_uses_storyboard_aspect_ratio(self):
        import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief = root / "brief.json"
            brief.write_text(json.dumps({"topic": "Vertical story", "tone": "professional"}), encoding="utf-8")
            (root / "storyboard.json").write_text(
                json.dumps({"output_profile": "vertical", "aspect_ratio": "9:16", "total_duration": 10}),
                encoding="utf-8",
            )
            pipeline._generate_thumbnail_prompt(root, brief)
            prompt = json.loads((root / "thumbnail_prompt.json").read_text(encoding="utf-8"))
            self.assertIn("9:16", prompt["prompt"])
            self.assertNotIn("4K, 16:9", prompt["prompt"])

    def test_design_agent_thumbnail_prompt_uses_storyboard_aspect_ratio(self):
        from engines.design_agent import generate_thumbnail_prompt

        storyboard = {"output_profile": "vertical", "aspect_ratio": "9:16", "total_duration": 10}
        prompt = generate_thumbnail_prompt(storyboard, {"topic": "Vertical story", "tone": "professional"})
        self.assertIn("9:16", prompt["prompt"])
        self.assertNotIn("4K, 16:9", prompt["prompt"])

    def test_generation_plan_rejects_malformed_status_types_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clips").mkdir()
            base = {
                "total_scenes": 1,
                "output_profile": "landscape",
                "aspect_ratio": "16:9",
                "resolution": "1920x1080",
                "scenes": [{
                    "scene_number": 1,
                    "duration_seconds": 1.0,
                    "status": "dry_run",
                    "target_file": "clips/scene_01.mp4",
                }],
            }
            for malformed in (
                {**base, "status": {}},
                {**base, "status": "dry_run", "scenes": [{**base["scenes"][0], "status": {}}]},
            ):
                (root / "clips" / "generation_plan.json").write_text(json.dumps(malformed), encoding="utf-8")
                result = _generation_plan_info(root)
                self.assertFalse(result["valid"])
                self.assertIsNone(result["status"])
                self.assertTrue(result["errors"])

    def test_generation_plan_rejects_mixed_dry_run_and_completed_scene_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clips").mkdir()
            (root / "clips" / "generation_plan.json").write_text(json.dumps({
                "status": "completed",
                "total_scenes": 1,
                "output_profile": "landscape",
                "aspect_ratio": "16:9",
                "resolution": "1920x1080",
                "scenes": [{
                    "scene_number": 1,
                    "duration_seconds": 1.0,
                    "status": "dry_run",
                    "target_file": "clips/scene_01.mp4",
                    "sha256": "0" * 64,
                }],
            }), encoding="utf-8")
            result = _generation_plan_info(root)
            self.assertFalse(result["valid"])

    def test_worker_thumbnail_prompt_uses_storyboard_aspect_ratio(self):
        import worker

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief = root / "brief.json"
            brief.write_text(json.dumps({"topic": "Vertical story", "tone": "professional"}), encoding="utf-8")
            (root / "storyboard.json").write_text(
                json.dumps({"output_profile": "vertical", "aspect_ratio": "9:16", "total_duration": 10}),
                encoding="utf-8",
            )
            worker._generate_thumbnail_for_job(root, brief)
            prompt = json.loads((root / "thumbnail_prompt.json").read_text(encoding="utf-8"))
            self.assertIn("9:16", prompt["prompt"])
            self.assertNotIn("4K, 16:9", prompt["prompt"])

    def test_normalize_output_profile_rejects_malformed_falsy_values(self):
        for value in (False, 0, [], {}, ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_output_profile(value)

        self.assertEqual(normalize_output_profile(None)["output_profile"], "landscape")

    def test_profile_contract_rejects_malformed_secondary_metadata(self):
        for payload in (
            {"output_profile": "vertical", "aspect_ratio": False},
            {"output_profile": "vertical", "resolution": "not-a-resolution"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    validate_output_profile_contract(payload, "test")

    def test_private_staging_rejects_directory_replaced_before_open(self):
        import package_utils

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            state = {}
            real_mkdir = package_utils.os.mkdir
            real_open = package_utils.os.open

            def mkdir_and_record(name, mode, *, dir_fd):
                real_mkdir(name, mode, dir_fd=dir_fd)
                if dir_fd == parent_fd and not state:
                    state["name"] = name

            def open_and_replace(name, flags, mode=0o777, *, dir_fd=None):
                descriptor = real_open(name, flags, mode, dir_fd=dir_fd)
                if dir_fd == parent_fd and name == state.get("name") and "swapped" not in state:
                    state["swapped"] = True
                    os.rename(name, "original-staging", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    real_mkdir(name, 0o700, dir_fd=parent_fd)
                    (root / name / "attacker-marker").write_text("must-survive", encoding="utf-8")
                return descriptor

            try:
                with patch("package_utils.os.mkdir", side_effect=mkdir_and_record), patch(
                    "package_utils.os.open", side_effect=open_and_replace
                ):
                    with self.assertRaises(OSError):
                        package_utils._open_private_staging_directory(parent_fd)
            finally:
                os.close(parent_fd)
            replacement = root / state["name"]
            self.assertEqual((replacement / "attacker-marker").read_text(encoding="utf-8"), "must-survive")

    def test_atomic_json_failure_preserves_replaced_temporary_inode(self):
        import package_utils

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"

            def fail_after_replacing_temp(_payload, handle, **_kwargs):
                temporary = Path(os.readlink(f"/proc/self/fd/{handle.fileno()}"))
                temporary.unlink()
                temporary.write_text("attacker-replacement", encoding="utf-8")
                raise OSError("injected writer failure")

            with patch("package_utils.json.dump", side_effect=fail_after_replacing_temp):
                with self.assertRaises(OSError):
                    package_utils.atomic_write_json(path, {"safe": True})
            leftovers = list(Path(tmp).glob(".staging-*"))
            self.assertTrue(leftovers)
            self.assertTrue(
                any(
                    child.is_file() and child.read_text(encoding="utf-8") == "attacker-replacement"
                    for item in leftovers
                    for child in item.iterdir()
                )
            )

    def test_atomic_text_failure_preserves_replaced_temporary_inode(self):
        import package_utils

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.txt"
            real_fsync = package_utils.os.fsync
            state = {}

            def fail_after_replacing_temp(fd):
                real_fsync(fd)
                if not state:
                    state["triggered"] = True
                    temporary = Path(os.readlink(f"/proc/self/fd/{fd}"))
                    temporary.unlink()
                    temporary.write_text("attacker-replacement", encoding="utf-8")
                    raise OSError("injected writer failure")

            with patch("package_utils.os.fsync", side_effect=fail_after_replacing_temp):
                with self.assertRaises(OSError):
                    package_utils.atomic_write_text(path, "trusted")
            leftovers = list(Path(tmp).glob(".staging-*"))
            self.assertTrue(leftovers)
            self.assertTrue(
                any(
                    child.is_file() and child.read_text(encoding="utf-8") == "attacker-replacement"
                    for item in leftovers
                    for child in item.iterdir()
                )
            )

    def test_cleanup_claim_rechecks_replacement_before_deletion(self):
        for kind in ("file", "symlink", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                entry = root / "victim"
                original = root / "original"
                if kind == "file":
                    entry.write_text("trusted", encoding="utf-8")
                elif kind == "symlink":
                    (root / "trusted-target").write_text("trusted", encoding="utf-8")
                    entry.symlink_to("trusted-target")
                else:
                    entry.mkdir()
                    (entry / "trusted.txt").write_text("trusted", encoding="utf-8")
                expected = package_utils._entry_cleanup_identity_at(os.open(root, os.O_RDONLY | os.O_DIRECTORY), "victim")
                parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                swapped = False
                real_rename = package_utils.os.rename
                try:
                    def replace_during_claim(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
                        nonlocal swapped
                        if not swapped and src == "victim" and dst == "entry" and src_dir_fd == parent_fd:
                            swapped = True
                            real_rename(entry, original)
                            if kind == "file":
                                entry.write_text("replacement", encoding="utf-8")
                            elif kind == "symlink":
                                entry.symlink_to("replacement-target")
                            else:
                                entry.mkdir()
                                (entry / "replacement.txt").write_text("replacement", encoding="utf-8")
                        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

                    with patch("package_utils.os.rename", side_effect=replace_during_claim):
                        with self.assertRaises(OSError):
                            package_utils._remove_entry_at(parent_fd, "victim", expected)
                finally:
                    os.close(parent_fd)
                self.assertTrue(original.exists())
                recovered = [path for path in root.iterdir() if path.name.startswith(".recovered-")]
                self.assertTrue(recovered)
                if kind == "file":
                    self.assertTrue(any(path.is_file() and path.read_text(encoding="utf-8") == "replacement" for path in recovered))
                elif kind == "symlink":
                    self.assertTrue(any(path.is_symlink() for path in recovered))
                else:
                    self.assertTrue(any(path.is_dir() and (path / "replacement.txt").read_text(encoding="utf-8") == "replacement" for path in recovered))

    def test_placeholder_cleanup_rechecks_replacement_before_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical"
            trusted = root / "trusted"
            canonical.write_text("attacker", encoding="utf-8")
            trusted.write_text("trusted", encoding="utf-8")
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            swapped = False
            real_rename = package_utils.os.rename
            try:
                expected = package_utils._entry_cleanup_identity_at(parent_fd, "trusted")

                def replace_placeholder(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
                    nonlocal swapped
                    if not swapped and src == "canonical" and dst == "entry" and src_dir_fd == parent_fd:
                        swapped = True
                        real_rename(root / "canonical", root / "placeholder-original")
                        canonical.write_text("replacement", encoding="utf-8")
                    return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

                with patch("package_utils.os.rename", side_effect=replace_placeholder):
                    with self.assertRaises(OSError):
                        package_utils._contain_entry_at(parent_fd, "canonical", expected, "test-placeholder")
            finally:
                os.close(parent_fd)
            self.assertFalse(canonical.exists())
            self.assertTrue((root / "placeholder-original").exists())
            self.assertTrue(any(path.is_file() and path.read_text(encoding="utf-8") == "replacement" for path in root.glob(".recovered-*")))

    def test_json_artifact_rejects_oversized_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oversized.json"
            with path.open("wb") as handle:
                handle.truncate(package_utils.MAX_JSON_BYTES + 1)
            with self.assertRaises(ValueError):
                package_utils.read_json_artifact(path)

    def test_cleanup_rejects_same_inode_replacement_with_reused_ctime(self):
        import package_utils

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / "victim"
            entry.write_text("trusted", encoding="utf-8")
            original = os.lstat(entry)
            entry.unlink()
            entry.write_text("replacement", encoding="utf-8")
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(OSError):
                    package_utils._remove_entry_at(parent_fd, entry.name, package_utils._cleanup_identity(original))
            finally:
                os.close(parent_fd)
            self.assertEqual(entry.read_text(encoding="utf-8"), "replacement")

    def test_cleanup_rejects_in_place_same_inode_mutation_during_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / "victim"
            entry.write_text("trusted", encoding="utf-8")
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            real_rename = package_utils.os.rename
            mutated = False
            try:
                expected = package_utils._cleanup_identity(os.lstat(entry))

                def mutate_during_claim(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
                    nonlocal mutated
                    if not mutated and src == "victim" and dst == "entry" and src_dir_fd == parent_fd:
                        mutated = True
                        entry.write_text("changed", encoding="utf-8")
                    return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

                with patch("package_utils.os.rename", side_effect=mutate_during_claim):
                    with self.assertRaises(OSError):
                        package_utils._remove_entry_at(parent_fd, entry.name, expected)
            finally:
                os.close(parent_fd)
            self.assertTrue(mutated)
            recovered = [path for path in root.iterdir() if path.name.startswith(".recovered-")]
            self.assertTrue(recovered)
            self.assertEqual(recovered[0].read_text(encoding="utf-8"), "changed")

    def test_atomic_writers_quarantine_canonical_replacement_during_verification(self):
        import package_utils

        for suffix, writer, payload in ((".json", package_utils.atomic_write_json, {"safe": True}), (".txt", package_utils.atomic_write_text, "trusted")):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"manifest{suffix}"
                path.write_text("old", encoding="utf-8")
                real_open = package_utils._open_regular_descriptor
                swapped = False

                def replace_before_verify(candidate):
                    nonlocal swapped
                    if not swapped and Path(candidate) == path:
                        swapped = True
                        path.unlink()
                        path.write_text("attacker-replacement", encoding="utf-8")
                        raise OSError("injected verification race")
                    return real_open(candidate)

                with patch("package_utils._open_regular_descriptor", side_effect=replace_before_verify):
                    with self.assertRaises(OSError):
                        if suffix == ".json":
                            cast(Any, writer)(path, payload)
                        else:
                            cast(Any, writer)(path, str(payload))
                self.assertFalse(path.exists())
                quarantined = [candidate for candidate in Path(tmp).iterdir() if candidate.name.startswith(".atomic-publication.untrusted-")]
                self.assertTrue(quarantined)
                self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "attacker-replacement")

        from package_utils import atomic_write_json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            errors = []

            def write(index):
                try:
                    atomic_write_json(path, {"writer": index})
                except Exception as exc:  # pragma: no cover - assertion captures the concrete failure
                    errors.append(exc)

            threads = [threading.Thread(target=write, args=(index,)) for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertIn("writer", json.loads(path.read_text()))

    def test_atomic_jobs_write_normalizes_insecure_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text("{}")
            path.chmod(0o666)
            atomic_write_json(path, {"safe": True})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o660)

    def test_non_mp4_infinite_duration_is_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voiceover.mp3"
            path.write_bytes(b"audio")
            with patch("package_utils.shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "package_utils._run_bounded_subprocess",
                return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [{"codec_type": "audio"}], "format": {"duration": "Infinity"}}), stderr=""),
            ):
                result = _probe_media(path)
            self.assertFalse(result["valid"])
            self.assertIn("finite", result["error"])

    def test_clear_generated_artifacts_removes_stale_media_but_preserves_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "brief.yaml").write_text("topic: keep me\n")
            (root / "visuals").mkdir()
            (root / "visuals" / "scene_01.png").write_bytes(b"old image")
            (root / "clips").mkdir()
            (root / "clips" / "scene_01.mp4").write_bytes(b"old clip")
            (root / "package_manifest.json").write_text("{}")

            removed = clear_generated_artifacts(root)

            self.assertTrue((root / "brief.yaml").is_file())
            self.assertFalse((root / "visuals").exists())
            self.assertFalse((root / "clips").exists())
            self.assertFalse((root / "package_manifest.json").exists())
            self.assertIn("visuals", removed)
            self.assertIn("clips", removed)

    def test_update_json_file_serializes_read_modify_write_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text(json.dumps({"seed": {"count": 0}}))

            def add_job(idx: int):
                def updater(jobs: dict) -> dict:
                    jobs[f"job-{idx}"] = {"id": f"job-{idx}"}
                    return jobs

                update_json_file(path, updater)

            threads = [threading.Thread(target=add_job, args=(idx,)) for idx in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            saved = json.loads(path.read_text())
            self.assertEqual(len([key for key in saved if key.startswith("job-")]), 20)

    def test_update_json_file_refuses_to_overwrite_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text('{"broken":')

            with self.assertRaises(ValueError):
                update_json_file(path, lambda jobs: {**jobs, "new": {"id": "new"}})

            self.assertEqual(path.read_text(), '{"broken":')

    def test_read_json_object_refuses_corrupt_or_non_object_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text('{"broken":')
            with self.assertRaises(ValueError):
                read_json_object(path)

            path.write_text('[{"id": "not-a-job-map"}]')
            with self.assertRaises(ValueError):
                read_json_object(path)

    def test_editor_package_status_requires_editor_artifacts_not_real_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "audio").mkdir()
            artifacts = {
                "creative_brief.json": "{}",
                "script.txt": "script",
                "storyboard.json": json.dumps({"scenes": [{"scene_number": 1}], "total_duration": 6}),
                "video_prompts.json": json.dumps({"scenes": []}),
                "audio/voiceover_script.txt": "hello",
                "music_prompt.txt": "music",
                "captions.srt": "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
                "assembly_manifest.json": "{}",
                "timeline.fcpxml": "<fcpxml></fcpxml>",
            }
            for rel, content in artifacts.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            status = compute_package_status(root, "completed")

            self.assertEqual(status["package_status"], "editor_package")
            self.assertFalse(status["has_clips"])
            self.assertFalse(status["has_final_video"])

    def test_manifest_records_prompt_only_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "video_prompts.json").write_text(json.dumps({"scenes": []}))

            manifest = write_package_manifest(root, {"id": "job1", "status": "completed"})

            self.assertEqual(manifest["package_status"], "prompt_package_only")
            self.assertEqual(manifest["job"]["package_status"], "prompt_package_only")
            self.assertFalse(manifest["job"]["has_final_video"])
            self.assertTrue((root / "package_manifest.json").is_file())
            saved = json.loads((root / "package_manifest.json").read_text())
            self.assertEqual(saved["package_status"], "prompt_package_only")
            self.assertEqual(saved["job"]["package_status"], saved["package_status"])

    def test_zero_byte_visual_does_not_count_as_visuals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "visuals").mkdir()
            (root / "visuals" / "scene_01.png").write_bytes(b"")

            status = compute_package_status(root, "completed")

            self.assertFalse(status["has_visuals"])

    def test_corrupt_png_visual_does_not_count_as_visuals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "visuals").mkdir()
            (root / "visuals" / "scene_01.png").write_bytes(b"not really a png")

            status = compute_package_status(root, "completed")

            self.assertFalse(status["has_visuals"])

    def test_ffprobe_timeout_is_nonfatal_and_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "final" / "video.mp4"
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")

            import subprocess
            with patch("package_utils.shutil.which", return_value="ffprobe"), patch(
                "package_utils._run_bounded_subprocess", side_effect=subprocess.TimeoutExpired("ffprobe", 15)
            ):
                status = compute_package_status(root, "completed")

            self.assertFalse(status["has_final_video"])
            self.assertEqual(status["package_status"], "not_started")
            self.assertEqual(status["final_video_probe"]["error"], "ffprobe timed out")

    def test_malformed_storyboard_is_reported_not_crashed_or_treated_as_editor_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "audio").mkdir()
            fixtures = {
                "creative_brief.json": "{}",
                "script.txt": "script",
                "storyboard.json": "[]",
                "video_prompts.json": json.dumps({"scenes": []}),
                "audio/voiceover_script.txt": "voiceover",
                "music_prompt.txt": "music",
                "captions.srt": "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
                "assembly_manifest.json": "{}",
                "timeline.fcpxml": "<fcpxml></fcpxml>",
            }
            for rel, content in fixtures.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            status = compute_package_status(root, "completed")

            self.assertEqual(status["expected_scenes"], 0)
            self.assertFalse(status["artifacts"]["storyboard"])
            self.assertIn("storyboard.json must be an object", status["artifact_errors"][0])
            self.assertNotEqual(status["package_status"], "editor_package")

            (root / "storyboard.json").write_text(json.dumps({"scenes": []}))
            status = compute_package_status(root, "completed")
            self.assertFalse(status["artifacts"]["storyboard"])
            self.assertNotEqual(status["package_status"], "editor_package")

    def test_clips_generated_requires_exact_expected_scene_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "storyboard.json").write_text(json.dumps({"scenes": [{}, {}]}))
            (root / "clips").mkdir()
            (root / "clips" / "scene_01.mp4").write_bytes(b"valid enough for mocked ffprobe")
            (root / "clips" / "scene_99.mp4").write_bytes(b"valid enough for mocked ffprobe")

            def fake_probe(path, **_kwargs):
                if path.name.startswith("scene_"):
                    return {"path": str(path), "exists": True, "size_bytes": 1, "ffprobe_checked": True, "valid": True, "duration_seconds": 1.0}
                return {"path": str(path), "exists": False, "size_bytes": 0, "ffprobe_checked": False, "valid": False}

            with patch("package_utils._probe_media", side_effect=fake_probe):
                status = compute_package_status(root, "completed")

            self.assertEqual(status["verified_clip_scene_numbers"], [1, 99])
            self.assertEqual(status["missing_clip_scene_numbers"], [2])
            self.assertEqual(status["extra_clip_scene_numbers"], [99])
            self.assertTrue(status["has_partial_clips"])
            self.assertFalse(status["has_clips"])
            self.assertNotEqual(status["package_status"], "clips_generated")

    def test_case_variant_clip_filenames_block_clip_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "storyboard.json").write_text(json.dumps({"scenes": [{}]}))
            (root / "clips").mkdir()
            (root / "clips" / "scene_01.mp4").write_bytes(b"valid enough for mocked ffprobe")
            (root / "clips" / "scene_99.MP4").write_bytes(b"valid enough for mocked ffprobe")

            def fake_probe(path, **_kwargs):
                if path.name.startswith("scene_"):
                    return {"path": str(path), "exists": True, "size_bytes": 1, "ffprobe_checked": True, "valid": True, "duration_seconds": 1.0}
                return {"path": str(path), "exists": False, "size_bytes": 0, "ffprobe_checked": False, "valid": False}

            with patch("package_utils._probe_media", side_effect=fake_probe):
                status = compute_package_status(root, "completed")

            self.assertEqual(status["verified_clip_scene_numbers"], [1])
            self.assertEqual(status["invalid_clip_files"], ["scene_99.MP4"])
            self.assertEqual(status["extra_clip_scene_numbers"], [])
            self.assertEqual(status["missing_clip_scene_numbers"], [])
            self.assertTrue(status["has_partial_clips"])
            self.assertFalse(status["has_clips"])
            self.assertNotEqual(status["package_status"], "clips_generated")

    def test_clip_lock_partial_and_temp_artifacts_block_complete_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "storyboard.json").write_text(json.dumps({"scenes": [{"scene_number": 1}]}))
            clips = root / "clips"
            clips.mkdir()
            clip = clips / "scene_01.mp4"
            clip.write_bytes(b"verified clip")
            (clips / "generation_plan.json").write_text(json.dumps({
                "status": "completed",
                "total_scenes": 1,
                "scenes": [{
                    "scene_number": 1,
                    "status": "verified",
                    "target_file": "clips/scene_01.mp4",
                    "sha256": hashlib.sha256(clip.read_bytes()).hexdigest(),
                }],
            }))
            artifact_names = ("scene_01.mp4.lock", "scene_01.mp4.part-worker", "scene_01.mp4.tmp")
            for name in artifact_names:
                (clips / name).write_bytes(b"transient artifact")
            (clips / "scene_01.mp4.symlink").symlink_to(clip)
            os.mkfifo(clips / "scene_01.mp4.fifo")
            invalid_names = sorted((*artifact_names, "scene_01.mp4.symlink", "scene_01.mp4.fifo"))

            def fake_probe(path, **_kwargs):
                if path.name == "scene_01.mp4":
                    return {"path": str(path), "exists": True, "size_bytes": 1, "ffprobe_checked": True, "valid": True, "duration_seconds": 1.0}
                return {"path": str(path), "exists": False, "size_bytes": 0, "ffprobe_checked": False, "valid": False}

            with patch("package_utils._probe_media", side_effect=fake_probe):
                status = compute_package_status(root, "completed")

            self.assertEqual(status["verified_clip_scene_numbers"], [1])
            self.assertEqual(status["invalid_clip_files"], invalid_names)
            self.assertFalse(status["has_clips"])
            self.assertNotEqual(status["package_status"], "clips_generated")

    def test_extra_verified_clip_prevents_clips_generated_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "storyboard.json").write_text(json.dumps({"scenes": [{}, {}]}))
            (root / "clips").mkdir()
            for scene_number in (1, 2, 99):
                (root / "clips" / f"scene_{scene_number:02d}.mp4").write_bytes(b"verified")

            def fake_probe(path, **_kwargs):
                if path.suffix == ".mp4":
                    return {"path": str(path), "exists": True, "size_bytes": 1, "ffprobe_checked": True, "valid": True, "duration_seconds": 1.0}
                return {"path": str(path), "exists": False, "size_bytes": 0, "ffprobe_checked": False, "valid": False}

            with patch("package_utils._probe_media", side_effect=fake_probe):
                status = compute_package_status(root, "completed")

            self.assertEqual(status["verified_clip_scene_numbers"], [1, 2, 99])
            self.assertEqual(status["extra_clip_scene_numbers"], [99])
            self.assertFalse(status["has_clips"])
            self.assertNotEqual(status["package_status"], "clips_generated")


class GenerationAgentTests(unittest.TestCase):
    def test_generation_agent_rejects_malformed_json_and_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed = root / "malformed.json"
            malformed.write_text("not json")
            self.assertEqual(generate_plan(malformed, root)["status"], "failed")

            prompts = root / "durations.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "duration_seconds": "NaN"}]}))
            plan = generate_plan(prompts, root)
            self.assertEqual(plan["status"], "failed")
            self.assertIn("finite positive", plan["reason"])

            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "duration_seconds": True}]}))
            plan = generate_plan(prompts, root)
            self.assertEqual(plan["status"], "failed")
            self.assertIn("finite positive", plan["reason"])

            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "seedance_prompt": 42}]}))
            plan = generate_plan(prompts, root)
            self.assertEqual(plan["status"], "failed")
            self.assertIn("must be strings", plan["reason"])

    def test_worker_and_api_share_configured_jobs_file(self):
        worker_source = (ROOT / "worker.py").read_text()
        api_source = (ROOT / "api.py").read_text()
        deploy_source = (ROOT / "deploy-traefik.sh").read_text()
        self.assertIn("SOLO_STUDIO_JOBS_FILE", worker_source)
        self.assertIn("SOLO_STUDIO_JOBS_FILE", api_source)
        self.assertIn("SOLO_STUDIO_JOBS_FILE=/app/state/jobs.json", deploy_source)

    def test_generation_agent_rejects_failed_array_provider_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "engines.generation_agent._run_bounded_subprocess",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([{"status": "failed", "result_url": "https://provider.example/video.mp4"}]),
                    stderr="",
                ),
            ), patch("engines.generation_agent._open_provider_url") as open_url:
                result = run_higgsfield("safe prompt", 5, Path(tmp) / "clip.mp4", "model")
            self.assertEqual(result["status"], "failed")
            open_url.assert_not_called()

    def test_generation_agent_writes_dry_run_plan_without_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clips").mkdir()
            (root / "clips" / "scene_01.mp4").write_bytes(b"stale clip")
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({
                "scenes": [
                    {
                        "scene_number": 1,
                        "duration_seconds": 6,
                        "runway_prompt": "A cinematic product shot.",
                        "seedance_prompt": "A Seedance-specific product shot.",
                        "kling_prompt": "Scene: A cinematic product shot. Camera movement: slow push.",
                        "transition": "cut",
                    }
                ]
            }))

            plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "dry_run")
            self.assertEqual(plan["total_scenes"], 1)
            self.assertTrue((root / "clips" / "generation_plan.json").is_file())
            self.assertFalse((root / "clips" / "scene_01.mp4").exists())
            self.assertIn("SOLO_STUDIO_ENABLE_HIGGSFIELD", plan["setup_needed"])
            self.assertEqual(plan["scenes"][0]["source_prompts"]["seedance"], "A Seedance-specific product shot.")

    def test_generation_agent_propagates_vertical_profile_in_dry_run_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({
                "output_profile": "vertical",
                "aspect_ratio": "9:16",
                "resolution": "1080x1920",
                "scenes": [{
                    "scene_number": 1,
                    "duration_seconds": 6,
                    "visual_description": "A founder presenting a phone app.",
                    "camera": "slow push in",
                }],
            }))

            plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "dry_run")
            self.assertEqual(plan["output_profile"], "vertical")
            self.assertEqual(plan["aspect_ratio"], "9:16")
            self.assertEqual(plan["resolution"], "1080x1920")
            self.assertEqual(plan["scenes"][0]["output_profile"], "vertical")
            self.assertIn("9:16 vertical video clip", plan["scenes"][0]["prompt"])

    def test_higgsfield_command_uses_requested_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "scene_01.mp4"
            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent._run_bounded_subprocess",
                return_value=SimpleNamespace(returncode=2, stdout="", stderr="provider failure"),
            ) as run:
                result = run_higgsfield("safe prompt", 5, out_file, "seedance_2_0", "9:16")

            self.assertEqual(result["status"], "failed")
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--aspect_ratio") + 1], "9:16")
            self.assertFalse(out_file.exists())

    def test_generation_agent_accepts_documented_array_provider_response(self):
        from engines.generation_agent import _provider_url

        payload = [{"status": "completed", "result": {"video_url": "https://provider.example/scene.mp4"}}]

        self.assertEqual(_provider_url(payload), "https://provider.example/scene.mp4")

    def test_generation_agent_rejects_malformed_scene_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1}, {"scene_number": 1}]}))

            plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertIn("unique positive integer", plan["reason"])

            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1}, {"scene_number": 3}]}))
            plan = generate_plan(prompts, root)
            self.assertEqual(plan["status"], "failed")
            self.assertIn("contiguous", plan["reason"])

    def test_generation_agent_clears_stale_clips_before_invalid_input_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            stale = clips / "scene_01.mp4"
            stale.write_bytes(b"stale")
            plan = generate_plan(root / "missing-video-prompts.json", root)
            self.assertEqual(plan["status"], "failed")
            self.assertTrue(stale.exists())

    def test_generation_agent_requires_video_stream_and_finite_duration(self):
        from engines.generation_agent import _verify_clip

        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "scene.mp4"
            clip.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")
            with patch("engines.generation_agent.shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "engines.generation_agent._run_bounded_subprocess",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"streams": [], "format": {"duration": "1.0"}}),
                    stderr="",
                ),
            ):
                valid, reason = _verify_clip(clip)
            self.assertFalse(valid)
            self.assertIn("no video stream", reason or "")

            with patch("engines.generation_agent.shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "engines.generation_agent._run_bounded_subprocess",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "NaN"}}),
                    stderr="",
                ),
            ):
                valid, reason = _verify_clip(clip)
            self.assertFalse(valid)
            self.assertIn("positive", reason or "")

    def test_generation_agent_real_higgsfield_mode_downloads_verified_scene_without_log_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({
                "scenes": [
                    {
                        "scene_number": 1,
                        "duration_seconds": 6,
                        "seedance_prompt": "A Seedance-specific product shot.",
                    }
                ]
            }))

            class FakeResponse:
                def __init__(self):
                    self._chunks = [b"fake mp4 bytes", b""]

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _size=-1):
                    return self._chunks.pop(0)

            provider_stdout = json.dumps({
                "result_url": "https://cdn.example.test/scene-01.mp4",
                "debug": "provider-token-should-not-be-persisted",
            })

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/local/bin/higgsfield"
            ), patch("engines.generation_agent._run_bounded_subprocess") as run, patch(
                "engines.generation_agent._open_provider_url", return_value=FakeResponse()
            ), patch(
                "engines.generation_agent._verify_clip", return_value=(True, None)
            ):
                run.return_value.returncode = 0
                run.return_value.stdout = provider_stdout
                run.return_value.stderr = "stderr-token-should-not-be-persisted"

                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "completed")
            self.assertEqual(plan["scenes"][0]["status"], "downloaded")
            self.assertEqual((root / "clips" / "scene_01.mp4").read_bytes(), b"fake mp4 bytes")
            serialized = json.dumps(plan)
            self.assertNotIn("provider-token-should-not-be-persisted", serialized)
            self.assertNotIn("stderr-token-should-not-be-persisted", serialized)
            self.assertNotIn("cdn.example.test", serialized)

    def test_generation_agent_submission_is_single_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "scene_01.mp4"
            with patch.dict("os.environ", {"SOLO_STUDIO_HIGGSFIELD_TIMEOUT": "1", "SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent._run_bounded_subprocess",
                return_value=SimpleNamespace(returncode=2, stdout="", stderr="provider failure"),
            ) as run:
                result = run_higgsfield("secret prompt", 5, out_file, "seedance_2_0")

            self.assertEqual(run.call_count, 1)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(out_file.exists())

    def test_generation_agent_rejects_invalid_provider_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "seedance_prompt": "scene"}]}))

            class FakeResponse:
                def __init__(self):
                    self._chunks = [b"not a valid mp4 artifact", b""]

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _size=-1):
                    return self._chunks.pop(0)

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which",
                side_effect=["/usr/bin/higgsfield", "/usr/bin/ffprobe"],
            ), patch(
                "engines.generation_agent._run_bounded_subprocess",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"result_url": "https://cdn.example.test/bad"}),
                    stderr="",
                ),
            ), patch("engines.generation_agent._open_provider_url", return_value=FakeResponse()):
                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertEqual(plan["scenes"][0]["status"], "failed")
            self.assertFalse((root / "clips" / "scene_01.mp4").exists())
            self.assertIn("non-MP4", plan["scenes"][0]["error"])

    def test_generation_agent_rejects_empty_provider_download_without_publishing_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "seedance_prompt": "scene"}]}))

            class EmptyResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _size=-1):
                    return b""

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/bin/higgsfield"
            ), patch(
                "engines.generation_agent._run_bounded_subprocess",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"result_url": "https://cdn.example.test/empty.mp4"}),
                    stderr="",
                ),
            ), patch("engines.generation_agent._open_provider_url", return_value=EmptyResponse()):
                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertEqual(plan["scenes"][0]["status"], "failed")
            self.assertFalse((root / "clips" / "scene_01.mp4").exists())
            self.assertIn("Video download failed", plan["scenes"][0]["error"])

    def test_generation_agent_missing_https_result_url_fails_without_provider_url_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "seedance_prompt": "scene"}]}))

            provider_stdout = json.dumps({
                "result_url": "http://internal.example.test/scene.mp4",
                "debug": "provider-debug-token-should-not-leak",
            })
            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/bin/higgsfield"
            ), patch("engines.generation_agent._run_bounded_subprocess") as run, patch(
                "engines.generation_agent._open_provider_url"
            ) as opener:
                run.return_value.returncode = 0
                run.return_value.stdout = provider_stdout
                run.return_value.stderr = "stderr-token-should-not-leak"

                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertIn("without an HTTPS video URL", plan["scenes"][0]["error"])
            self.assertFalse((root / "clips" / "scene_01.mp4").exists())
            opener.assert_not_called()
            serialized = json.dumps(plan)
            self.assertNotIn("internal.example.test", serialized)
            self.assertNotIn("provider-debug-token-should-not-leak", serialized)
            self.assertNotIn("stderr-token-should-not-leak", serialized)

    def test_generation_agent_nonzero_provider_exit_does_not_persist_provider_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": [{"scene_number": 1, "seedance_prompt": "scene"}]}))

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/bin/higgsfield"
            ), patch("engines.generation_agent._run_bounded_subprocess") as run, patch(
                "engines.generation_agent._open_provider_url"
            ) as opener:
                run.return_value.returncode = 2
                run.return_value.stdout = "stdout-token-should-not-leak"
                run.return_value.stderr = "stderr-token-should-not-leak"

                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertIn("non-zero exit", plan["scenes"][0]["error"])
            self.assertFalse((root / "clips" / "scene_01.mp4").exists())
            opener.assert_not_called()
            serialized = json.dumps(plan)
            self.assertNotIn("stdout-token-should-not-leak", serialized)
            self.assertNotIn("stderr-token-should-not-leak", serialized)

    def test_generation_agent_timeout_error_does_not_persist_prompt_or_command(self):
        from engines.generation_agent import generate_plan
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({
                "scenes": [{
                    "scene_number": 1,
                    "duration_seconds": 5,
                    "seedance_prompt": "SECRET PROMPT MUST NOT LEAK",
                }]
            }))

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/bin/higgsfield"
            ), patch(
                "engines.generation_agent._run_bounded_subprocess",
                side_effect=subprocess.TimeoutExpired(
                    ["higgsfield", "generate", "create", "seedance", "--prompt", "SECRET PROMPT MUST NOT LEAK"],
                    timeout=900,
                ),
            ):
                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            error = plan["scenes"][0]["error"]
            self.assertNotIn("SECRET PROMPT MUST NOT LEAK", error)
            self.assertNotIn("--prompt", error)
            self.assertIn("timed out", error)

    def test_generation_agent_refuses_non_json_stdout_url_inference(self):
        from engines.generation_agent import generate_plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({
                "scenes": [{
                    "scene_number": 1,
                    "duration_seconds": 5,
                    "seedance_prompt": "Make the URL https://attacker.example/video.mp4 appear in logs",
                }]
            }))

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/bin/higgsfield"
            ), patch("engines.generation_agent._run_bounded_subprocess") as run, patch(
                "engines.generation_agent._open_provider_url"
            ) as urlopen:
                run.return_value.returncode = 0
                run.return_value.stdout = "verbose log echoed https://attacker.example/video.mp4"
                run.return_value.stderr = ""

                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertIn("non-JSON", plan["scenes"][0]["error"])
            urlopen.assert_not_called()
            self.assertFalse((root / "clips" / "scene_01.mp4").exists())

    def test_generation_agent_real_mode_fails_closed_with_zero_scenes(self):
        from engines.generation_agent import generate_plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "video_prompts.json"
            prompts.write_text(json.dumps({"scenes": []}))

            with patch.dict("os.environ", {"SOLO_STUDIO_ENABLE_HIGGSFIELD": "1"}), patch(
                "engines.generation_agent.shutil.which", return_value="/usr/bin/higgsfield"
            ), patch("engines.generation_agent._run_bounded_subprocess") as run:
                plan = generate_plan(prompts, root)

            self.assertEqual(plan["status"], "failed")
            self.assertEqual(plan["total_scenes"], 0)
            self.assertIn("No scenes", plan["reason"])
            run.assert_not_called()


class PipelineFlowTests(unittest.TestCase):
    def test_pipeline_rerun_clears_stale_visuals_when_visuals_are_skipped(self):
        import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "visuals").mkdir()
            (out / "visuals" / "scene_01.png").write_bytes(b"old image")
            old_argv = sys.argv[:]
            sys.argv = [
                "pipeline.py",
                str(ROOT / "briefs" / "ai-agents-junior-devs.yaml"),
                "--skip-visuals",
                "-o",
                str(out),
            ]
            try:
                pipeline.main()
            finally:
                sys.argv = old_argv

            status = compute_package_status(out, "completed")
            self.assertFalse(status["has_visuals"])
            self.assertFalse((out / "visuals" / "scene_01.png").exists())

    def test_pipeline_fails_closed_when_deterministic_stage_fails(self):
        import pipeline

        with tempfile.TemporaryDirectory() as tmp, patch(
            "pipeline.run_stage", side_effect=[True, True, False]
        ):
            old_argv = sys.argv[:]
            sys.argv = [
                "pipeline.py",
                str(ROOT / "briefs" / "ai-agents-junior-devs.yaml"),
                "--skip-visuals",
                "-o",
                tmp,
            ]
            try:
                with self.assertRaises(SystemExit) as exc:
                    pipeline.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(exc.exception.code, 1)
            manifest = Path(tmp) / "package_manifest.json"
            self.assertTrue(manifest.is_file())
            saved = json.loads(manifest.read_text())
            self.assertEqual(saved["job"]["status"], "failed")
            self.assertEqual(saved["package_status"], "failed")

    def test_pipeline_fails_closed_when_success_manifest_write_fails(self):
        import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            def fake_run_stage(name, *_args):
                if name == "4. Production Agent":
                    (Path(tmp) / "video_prompts.json").write_text(json.dumps({"scenes": []}))
                return True

            patchers = patch("pipeline.run_stage", side_effect=fake_run_stage), patch(
            "pipeline._generate_thumbnail_prompt", return_value=None
            ), patch("pipeline.write_package_manifest", side_effect=OSError("disk full"))
            old_argv = sys.argv[:]
            sys.argv = [
                "pipeline.py",
                str(ROOT / "briefs" / "ai-agents-junior-devs.yaml"),
                "--skip-visuals",
                "-o",
                tmp,
            ]
            with patchers[0], patchers[1], patchers[2]:
                try:
                    with self.assertRaises(SystemExit) as exc:
                        pipeline.main()
                finally:
                    sys.argv = old_argv

            self.assertEqual(exc.exception.code, 1)


class WorkerFlowTests(unittest.TestCase):
    def test_worker_media_helpers_do_not_claim_prompt_only_artifacts_are_real_media(self):
        import worker

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "audio").mkdir(parents=True)
            (root / "visual_prompts.json").write_text(json.dumps({"prompts": [{"scene_number": 1}]}))
            (root / "audio" / "voiceover_script.txt").write_text("voiceover text")

            self.assertFalse(worker._generate_visuals(root, {"scenes": [{"scene_number": 1}]}))
            self.assertFalse(worker._generate_voiceover(root, {"scenes": []}))
            self.assertFalse((root / "audio" / "voiceover.mp3").exists())

            self.assertFalse(worker._generate_visuals(Path(tmp) / "missing-visuals", {"scenes": []}))
            self.assertFalse(worker._generate_voiceover(Path(tmp) / "missing-voiceover", {"scenes": []}))

    def test_worker_early_stage_failures_write_package_manifest(self):
        import worker

        old_jobs_file = worker.JOBS_FILE
        old_output_root = worker.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker.JOBS_FILE = root / "jobs.json"
            worker.OUTPUT_ROOT = root / "output"
            job = {"id": "research-fail", "topic": "Research fails", "status": "queued"}
            update_json_file(worker.JOBS_FILE, lambda _jobs: {job["id"]: job})
            try:
                with patch("worker.run_stage", return_value=False):
                    worker.process_job(job["id"], job)
            finally:
                worker.JOBS_FILE = old_jobs_file
                worker.OUTPUT_ROOT = old_output_root

            manifest = root / "output" / job["id"] / "package_manifest.json"
            self.assertTrue(manifest.is_file())
            saved = json.loads(manifest.read_text())
            self.assertEqual(saved["job"]["status"], "failed")
            self.assertEqual(saved["package_status"], "failed")

    def test_worker_failure_manifest_uses_latest_persisted_job_snapshot(self):
        import worker

        old_jobs_file = worker.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker.JOBS_FILE = root / "jobs.json"
            original = {
                "id": "latest-snapshot",
                "topic": "Original job",
                "status": "queued",
                "stage": "waiting",
                "progress": 0.0,
                "package_status": "not_started",
            }
            persisted = {
                **original,
                "status": "running",
                "stage": "script",
                "progress": 0.14,
                "format": "short",
                "chapters": 3,
                "scenes": 5,
            }
            update_json_file(worker.JOBS_FILE, lambda _jobs: {original["id"]: persisted})
            try:
                worker._fail_job(original["id"], root / "output" / original["id"], original, "boom")
            finally:
                worker.JOBS_FILE = old_jobs_file

            saved = json.loads((root / "output" / original["id"] / "package_manifest.json").read_text())
            self.assertEqual(saved["job"]["status"], "failed")
            self.assertEqual(saved["job"]["stage"], "script")
            self.assertEqual(saved["job"]["progress"], 0.14)
            self.assertEqual(saved["job"]["format"], "short")
            self.assertEqual(saved["job"]["scenes"], 5)
            self.assertEqual(saved["job"]["package_status"], saved["package_status"])

    def test_sqlite_manifest_snapshot_reads_job_directly_not_capped_list(self):
        import worker

        fallback = {"id": "old-job", "status": "queued"}
        persisted = {"id": "old-job", "status": "completed", "final_video_sha256": "a" * 64}
        with patch.object(worker, "DATABASE_CONFIGURED", True), patch.object(
            worker.job_store, "get_job", return_value=persisted
        ) as get_job:
            snapshot = worker._manifest_job_snapshot("old-job", fallback)

        get_job.assert_called_once_with("old-job", path=worker.DATABASE_FILE)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["final_video_sha256"], "a" * 64)

    def test_worker_stage_timeout_returns_false_instead_of_crashing(self):
        import subprocess
        import worker

        with patch("worker.subprocess.run", side_effect=subprocess.TimeoutExpired("stage", 300)):
            self.assertFalse(worker.run_stage("job-timeout", "Slow Stage", "script_agent.py", "arg"))

    def test_worker_failure_finalizer_does_not_recurse_on_corrupt_jobs_file(self):
        import worker

        old_jobs_file = worker.JOBS_FILE
        old_output_root = worker.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker.JOBS_FILE = root / "jobs.json"
            worker.OUTPUT_ROOT = root / "output"
            worker.JOBS_FILE.write_text('{"broken":')
            try:
                worker.process_job("corrupt-job", {"id": "corrupt-job", "topic": "Bad state"})
            finally:
                worker.JOBS_FILE = old_jobs_file
                worker.OUTPUT_ROOT = old_output_root

            manifest = root / "output" / "corrupt-job" / "package_manifest.json"
            self.assertTrue(manifest.is_file())
            self.assertEqual((root / "jobs.json").read_text(), '{"broken":')

    def test_worker_failure_manifest_survives_unreadable_sqlite_cancellation_state(self):
        import worker

        old_database_configured = worker.DATABASE_CONFIGURED
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = {"id": "corrupt-sqlite", "topic": "Bad database", "status": "running"}
            try:
                worker.DATABASE_CONFIGURED = True
                with patch("worker._job_is_cancelled", side_effect=OSError("database is corrupt")), patch(
                    "worker.load_jobs", return_value={}
                ), patch("worker.update_job", side_effect=OSError("database is corrupt")):
                    worker._fail_job(job["id"], root / job["id"], job, "stage failed")
            finally:
                worker.DATABASE_CONFIGURED = old_database_configured

            self.assertTrue((root / job["id"] / "package_manifest.json").is_file())

    def test_worker_failure_finalizer_persists_fallback_when_manifest_write_fails(self):
        import worker

        old_jobs_file = worker.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker.JOBS_FILE = root / "jobs.json"
            job = {"id": "manifest-fail", "topic": "Manifest write fails", "status": "queued"}
            update_json_file(worker.JOBS_FILE, lambda _jobs: {job["id"]: job})
            try:
                with patch("worker.write_package_manifest", side_effect=OSError("disk full")):
                    worker._fail_job(job["id"], root / "output" / job["id"], job, "boom")
            finally:
                worker.JOBS_FILE = old_jobs_file

            saved = json.loads((root / "jobs.json").read_text())[job["id"]]
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["package_status"], "failed")
            self.assertFalse(saved["has_final_video"])

    def test_worker_failure_finalizer_swallows_jobs_write_oserror(self):
        import worker

        with tempfile.TemporaryDirectory() as tmp:
            job = {"id": "jobs-write-fail", "topic": "Jobs write fails", "status": "queued"}
            with patch("worker.update_job", side_effect=OSError("lock file permission denied")):
                worker._fail_job(job["id"], Path(tmp) / job["id"], job, "boom")

            self.assertTrue((Path(tmp) / job["id"] / "package_manifest.json").is_file())

    def test_worker_poll_loop_survives_jobs_store_oserror(self):
        import worker

        with patch("worker.load_jobs", side_effect=OSError("lock file permission denied")), patch(
            "worker.time.sleep", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                worker.main()

    def test_package_status_rejects_symlinked_storyboard_and_visuals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "job"
            root.mkdir()
            external_storyboard = Path(tmp) / "external-storyboard.json"
            external_storyboard.write_text(json.dumps({"scenes": [{"scene_number": 1}]}))
            (root / "storyboard.json").symlink_to(external_storyboard)
            (root / "visuals").mkdir()
            external_visual = Path(tmp) / "external.png"
            external_visual.write_bytes(b"\x89PNG\r\n\x1a\nexternal")
            (root / "visuals" / "scene_01.png").symlink_to(external_visual)

            summary = compute_package_status(root)

            self.assertFalse(summary["artifacts"]["storyboard"])
            self.assertEqual(summary["expected_scenes"], 0)
            self.assertFalse(summary["has_visuals"])


class FrontendContractTests(unittest.TestCase):
    def test_pipeline_step_placeholders_match_stage_array(self):
        html = (ROOT / "frontend" / "index.html").read_text()
        block = re.search(
            r'<div class="pipeline" id="pipeline-steps">(.*?)\n      </div>\n\n      <div class="stats">',
            html,
            re.S,
        )
        if block is None:
            self.fail("frontend pipeline DOM block not found")
        placeholders = block.group(1).count('class="pipeline-step"')

        self.assertEqual(placeholders, len(job_store.DEFAULT_STAGE_NAMES))
        self.assertIn("job.stages.map(stage => stage && stage.stage_name)", html)
        self.assertIn("editing: 'Editing scenes into the final timeline...'", html)

    def test_download_artifact_pills_are_derived_from_artifact_summary(self):
        html = (ROOT / "frontend" / "index.html").read_text()
        function = re.search(r"function showDownloadView\(job\) \{(.*?)\n\}\n\n// ── Recent jobs", html, re.S)
        if function is None:
            self.fail("showDownloadView function not found")
        body = function.group(1)

        self.assertIn("const artifacts = job.artifact_summary || {}", body)
        self.assertNotIn("ok: true", body)
        self.assertIn("artifacts.creative_brief", body)
        self.assertIn("artifacts.video_prompts", body)
        self.assertIn("artifacts.assembly_manifest", body)
        self.assertIn("{ label: 'Scene Images', ok: job.has_visuals }", body)
        self.assertNotIn("job.has_visuals || artifacts.visual_prompts", body)
        self.assertIn("Available artifacts are marked below", html)

    def test_template_loader_has_no_debug_banner_or_step_diagnostics(self):
        html = (ROOT / "frontend" / "index.html").read_text()

        self.assertNotIn("JS init section reached OK", html)
        self.assertNotIn("Loading templates (step", html)
        self.assertIn("Loading templates...", html)
        self.assertNotIn("escapeJsAttr", html)
        self.assertNotIn("onclick=\"viewJob('${j.id}')\"", html)
        self.assertNotIn("onclick=\"selectTemplate", html)
        self.assertNotIn("onclick=\"quickStart", html)
        self.assertIn("safeClassToken", html)
        self.assertIn("addEventListener('click'", html)

    def test_frontend_uses_cookie_session_not_browser_token_storage_for_protected_job_routes(self):
        html = (ROOT / "frontend" / "index.html").read_text()

        self.assertIn("id=\"api-token\"", html)
        self.assertIn("function loginWithApiToken", html)
        self.assertIn("function protectedFetch", html)
        self.assertIn("credentials: 'same-origin'", html)
        self.assertIn("/auth/session", html)
        self.assertNotIn("API_TOKEN_STORAGE_KEY", html)
        self.assertNotIn("headers.set('Authorization', 'Bearer ' + token)", html)
        self.assertNotIn("sessionStorage", html)
        self.assertNotIn("localStorage", html)
        self.assertIn("protectedFetch('/jobs'", html)
        self.assertIn("protectedFetch('/jobs/' + currentJobId)", html)
        self.assertIn("protectedFetch('/jobs?limit=10')", html)
        self.assertIn("protectedFetch('/jobs/from-template/'", html)
        self.assertIn("protectedFetch('/jobs/' + encodeURIComponent(jobId) + '/download')", html)
        self.assertIn("filenameFromDisposition", html)
        self.assertIn("res.headers.get('Content-Disposition')", html)
        self.assertNotIn("id=\"download-link\"", html)


class ApiPackageStatusTests(unittest.TestCase):
    def test_health_endpoint_is_available_for_deploy_smoke(self):
        from fastapi.testclient import TestClient
        import api

        client = TestClient(api.app)
        response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "solo-studio-video")

    def test_video_prefixed_api_routes_work_for_direct_container_smoke(self):
        from fastapi.testclient import TestClient
        import api

        client = TestClient(api.app)

        response = client.get("/video/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "solo-studio-video")

    def test_token_gates_job_routes_but_leaves_health_and_templates_public(self):
        from fastapi.testclient import TestClient
        import api

        old_token = api.API_TOKEN
        old_jobs_file = api.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                test_token = "test-" + "secret"
                api.API_TOKEN = test_token
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(api.JOBS_FILE, lambda _jobs: {})
                client = TestClient(api.app)

                self.assertEqual(client.get("/api/health").status_code, 200)
                self.assertEqual(client.get("/api/templates").status_code, 200)
                self.assertEqual(client.get("/api/jobs").status_code, 401)
                self.assertEqual(client.get("/video/api/jobs").status_code, 401)
                self.assertEqual(
                    client.get("/api/jobs", headers={"Authorization": "Bearer wrong"}).status_code,
                    401,
                )
                self.assertEqual(
                    client.get("/api/jobs", headers={"Authorization": f"Bearer {test_token}"}).status_code,
                    200,
                )
                self.assertEqual(
                    client.get("/video/api/jobs", headers={"X-Solo-Studio-Token": test_token}).status_code,
                    200,
                )
                self.assertEqual(
                    client.post(
                        "/api/jobs",
                        headers={"Authorization": f"Bearer {test_token}", "Origin": "https://evil.example"},
                        json={"topic": "cross-origin mutation"},
                    ).status_code,
                    403,
                )
                self.assertEqual(client.get("/api/jobs/missing").status_code, 401)
                self.assertEqual(
                    client.get("/api/jobs/missing", headers={"Authorization": f"Bearer {test_token}"}).status_code,
                    404,
                )
            finally:
                api.API_TOKEN = old_token
                api.JOBS_FILE = old_jobs_file

    def test_create_job_rejects_boolean_or_non_finite_duration_minutes(self):
        from fastapi.testclient import TestClient
        import api

        old_token = api.API_TOKEN
        old_jobs_file = api.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                test_token = "test-" + "secret"
                api.API_TOKEN = test_token
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(api.JOBS_FILE, lambda _jobs: {})
                client = TestClient(api.app)
                headers = {"Authorization": f"Bearer {test_token}"}

                bool_response = client.post(
                    "/api/jobs",
                    headers=headers,
                    json={"topic": "bad", "duration_minutes": True},
                )
                self.assertIn(bool_response.status_code, [400, 422])

                nan_payload = "{\"topic\": \"bad\", \"duration_minutes\": NaN}"
                nan_response = client.post(
                    "/api/jobs",
                    headers={**headers, "Content-Type": "application/json"},
                    content=nan_payload,
                )
                self.assertNotEqual(nan_response.status_code, 500)
                self.assertIn(nan_response.status_code, [400, 422])
            finally:
                api.API_TOKEN = old_token
                api.JOBS_FILE = old_jobs_file

    def test_template_job_rejects_invalid_duration_with_unprocessable_entity(self):
        from fastapi.testclient import TestClient
        import api

        old_token = api.API_TOKEN
        old_jobs_file = api.JOBS_FILE
        old_api_file = api.__file__
        old_attempts = {key: list(values) for key, values in api.SESSION_LOGIN_ATTEMPTS.items()}
        with tempfile.TemporaryDirectory() as tmp:
            try:
                test_token = "test-" + "secret"
                api.API_TOKEN = test_token
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(api.JOBS_FILE, lambda _jobs: {})
                (Path(tmp) / "templates.json").write_text(json.dumps([
                    {
                        "id": "bad-duration",
                        "topic": "bad",
                        "target_audience": "general",
                        "duration_minutes": True,
                        "platform": "youtube",
                        "tone": "professional",
                    },
                    {
                        "id": "oversized-duration",
                        "topic": "bad",
                        "target_audience": "general",
                        "duration_minutes": 10**4000,
                        "platform": "youtube",
                        "tone": "professional",
                    }
                ]))
                api.__file__ = str(Path(tmp) / "api.py")
                client = TestClient(api.app)

                bool_response = client.post(
                    "/api/jobs/from-template/bad-duration",
                    headers={"Authorization": f"Bearer {test_token}"},
                )

                self.assertEqual(bool_response.status_code, 422)
                self.assertEqual(bool_response.json()["detail"], "duration_minutes must be a real number")

                oversized_response = client.post(
                    "/api/jobs/from-template/oversized-duration",
                    headers={"Authorization": f"Bearer {test_token}"},
                )

                self.assertEqual(oversized_response.status_code, 422)
                self.assertEqual(oversized_response.json()["detail"], "duration_minutes must be a number")
            finally:
                api.API_TOKEN = old_token
                api.JOBS_FILE = old_jobs_file
                api.__file__ = old_api_file
                api.SESSION_LOGIN_ATTEMPTS.clear()
                api.SESSION_LOGIN_ATTEMPTS.update(old_attempts)

    def test_api_operator_token_login_sets_httponly_cookie_and_job_routes_accept_cookie(self):
        from fastapi.testclient import TestClient
        import api

        old_token = api.API_TOKEN
        old_jobs_file = api.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                test_token = "test-" + "secret"
                api.API_TOKEN = test_token
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(api.JOBS_FILE, lambda _jobs: {})
                client = TestClient(api.app)

                same_origin = {"Origin": "https://edgescout.tech"}
                response = client.post("/api/auth/session", headers=same_origin, json={"token": test_token})
                self.assertEqual(response.status_code, 204)
                cookie = response.headers.get("set-cookie", "")
                self.assertIn("solo_studio_session=", cookie)
                self.assertIn("HttpOnly", cookie)
                self.assertIn("SameSite", cookie)
                self.assertNotIn(test_token, cookie)

                self.assertEqual(client.get("/api/jobs").status_code, 200)

                logout = client.post("/api/auth/logout", headers=same_origin)
                self.assertEqual(logout.status_code, 204)
                self.assertEqual(client.get("/api/jobs").status_code, 401)
            finally:
                api.API_TOKEN = old_token
                api.JOBS_FILE = old_jobs_file

    def test_api_session_login_rate_limit_and_expired_session_fail_closed(self):
        from fastapi.testclient import TestClient
        import api
        import time

        old_token = api.API_TOKEN
        old_jobs_file = api.JOBS_FILE
        old_attempts = api.SESSION_LOGIN_ATTEMPTS.copy()
        old_max_attempts = api.SESSION_LOGIN_MAX_ATTEMPTS
        old_cookie_secure = api.COOKIE_SECURE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                api.API_TOKEN = "test-" + "secret"
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(api.JOBS_FILE, lambda _jobs: {})
                api.SESSION_LOGIN_ATTEMPTS.clear()
                api.SESSION_LOGIN_MAX_ATTEMPTS = 2
                api.COOKIE_SECURE = True
                client = TestClient(api.app)

                same_origin = {"Origin": "https://edgescout.tech"}
                self.assertEqual(client.post("/api/auth/session", headers=same_origin, json={"token": "wrong"}).status_code, 401)
                self.assertEqual(client.post("/api/auth/session", headers=same_origin, json={"token": "wrong"}).status_code, 401)
                limited = client.post("/api/auth/session", headers=same_origin, json={"token": "wrong"})
                self.assertEqual(limited.status_code, 429)
                self.assertIn("Retry-After", limited.headers)

                client.cookies.set(api.SESSION_COOKIE_NAME, "expired-session")
                api.SESSION_TOKENS["expired-session"] = time.time() - 1
                api.SESSION_LOGIN_ATTEMPTS.clear()
                self.assertEqual(client.get("/api/jobs").status_code, 401)
            finally:
                api.API_TOKEN = old_token
                api.JOBS_FILE = old_jobs_file
                api.SESSION_LOGIN_ATTEMPTS.clear()
                api.SESSION_LOGIN_ATTEMPTS.update(old_attempts)
                api.SESSION_LOGIN_MAX_ATTEMPTS = old_max_attempts
                api.COOKIE_SECURE = old_cookie_secure

    def test_api_cors_is_not_wildcard_by_default(self):
        source = (ROOT / "api.py").read_text()

        self.assertIn("SOLO_STUDIO_CORS_ORIGINS", source)
        self.assertIn("SOLO_STUDIO_REQUIRE_API_TOKEN", source)
        self.assertIn("RuntimeError", source)
        self.assertIn("allow_origins=ALLOWED_ORIGINS", source)
        self.assertIn("REQUIRE_API_TOKEN or API_TOKEN", source)
        self.assertIn("SESSION_LOGIN_MAX_ATTEMPTS", source)
        self.assertNotIn("allow_origins=[\"*\"]", source)

    def test_save_jobs_is_atomic_and_create_flow_does_not_overwrite_worker_updates(self):
        import api

        old_jobs_file = api.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                api.JOBS_FILE = Path(tmp) / "jobs.json"
                update_json_file(
                    api.JOBS_FILE,
                    lambda _jobs: {"old": {"id": "old", "status": "running", "created_at": "t1"}},
                )

                api._add_job_locked("new", {"id": "new", "status": "queued", "created_at": "t2"})

                saved = json.loads(api.JOBS_FILE.read_text())
                self.assertEqual(saved["old"]["status"], "running")
                self.assertEqual(saved["new"]["status"], "queued")
            finally:
                api.JOBS_FILE = old_jobs_file

    def test_api_corrupt_jobs_store_returns_503_instead_of_empty_state(self):
        from fastapi.testclient import TestClient
        import api

        old_jobs_file = api.JOBS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            api.JOBS_FILE = Path(tmp) / "jobs.json"
            api.JOBS_FILE.write_text('{"broken":')
            try:
                client = TestClient(api.app)
                self.assertEqual(client.get("/api/jobs").status_code, 503)
                self.assertEqual(client.get("/api/jobs/known-job").status_code, 503)
                response = client.post("/api/jobs", json={
                    "topic": "x",
                    "target_audience": "y",
                    "duration_minutes": 1,
                    "platform": "youtube",
                    "tone": "professional",
                })
                self.assertEqual(response.status_code, 503)
                self.assertEqual(api.JOBS_FILE.read_text(), '{"broken":')
            finally:
                api.JOBS_FILE = old_jobs_file

    def test_api_jobs_store_oserror_returns_503_instead_of_raw_500(self):
        from fastapi.testclient import TestClient
        import api

        client = TestClient(api.app)
        with patch("api.read_json_object", side_effect=OSError("lock file permission denied")):
            self.assertEqual(client.get("/api/jobs").status_code, 503)
            self.assertEqual(client.get("/api/jobs/known-job").status_code, 503)

        with patch("api.update_json_file", side_effect=OSError("lock file permission denied")):
            response = client.post("/api/jobs", json={
                "topic": "x",
                "target_audience": "y",
                "duration_minutes": 1,
                "platform": "youtube",
                "tone": "professional",
            })
            self.assertEqual(response.status_code, 503)

    def test_api_artifact_enrichment_uses_threadpool_from_async_routes(self):
        source = (ROOT / "api.py").read_text()
        self.assertIn("from starlette.concurrency import run_in_threadpool", source)
        self.assertIn("return await run_in_threadpool(_enrich_jobs", source)
        self.assertIn("return await run_in_threadpool(_enrich_job", source)
        self.assertIn("await run_in_threadpool(_write_download_manifest", source)
        self.assertIn("def _enrich_jobs", source)
        self.assertIn("def _write_download_manifest", source)

    def test_job_status_and_download_include_artifact_manifest(self):
        from fastapi.testclient import TestClient
        import api

        old_jobs_file = api.JOBS_FILE
        old_output_root = api.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_id = "api-test-job"
            output = root / "output" / job_id
            (output / "audio").mkdir(parents=True)
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
                path = output / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            api.JOBS_FILE = root / "jobs.json"
            api.OUTPUT_ROOT = root / "output"
            update_json_file(api.JOBS_FILE, lambda _jobs: {
                job_id: {
                    "id": job_id,
                    "topic": "API package test",
                    "status": "completed",
                    "duration_seconds": 60,
                    "created_at": "2026-08-17T00:00:00+00:00",
                }
            })

            try:
                client = TestClient(api.app)
                status_response = client.get(f"/api/jobs/{job_id}")
                self.assertEqual(status_response.status_code, 200)
                self.assertEqual(status_response.json()["package_status"], "editor_package")

                download_response = client.get(f"/api/jobs/{job_id}/download")
                self.assertEqual(download_response.status_code, 200)
                package = zipfile.ZipFile(io.BytesIO(download_response.content))
                self.assertIn("package_manifest.json", package.namelist())
                manifest = json.loads(package.read("package_manifest.json"))
                self.assertEqual(manifest["package_status"], "editor_package")
            finally:
                api.JOBS_FILE = old_jobs_file
                api.OUTPUT_ROOT = old_output_root

    def test_write_brief_yaml_uses_safe_yaml_for_quotes_and_newlines(self):
        import yaml
        import api

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brief.yaml"
            job = {
                "topic": "Quoted \"topic\"\nwith newline",
                "target_audience": "founders: CTOs",
                "duration_seconds": 90,
                "platform": "youtube",
                "tone": "educational",
                "key_messages": ["Ship faster: review harder", "Line\nbreak"],
                "visual_style": "dark: cinematic",
                "call_to_action": "Subscribe \"now\"",
            }

            api._write_brief_yaml(path, job)
            parsed = yaml.safe_load(path.read_text())

            self.assertEqual(parsed["topic"], job["topic"])
            self.assertEqual(parsed["key_messages"], job["key_messages"])
            self.assertEqual(parsed["visual_style"], job["visual_style"])

    def test_write_brief_yaml_carries_output_profile_with_landscape_default(self):
        import yaml
        import api

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_job = {
                "topic": "Profile test",
                "target_audience": "founders",
                "duration_seconds": 60,
                "platform": "youtube",
                "tone": "professional",
                "key_messages": [],
                "visual_style": "",
                "call_to_action": "",
            }

            default_path = root / "default.yaml"
            api._write_brief_yaml(default_path, base_job)
            default_payload = yaml.safe_load(default_path.read_text())

            vertical_path = root / "vertical.yaml"
            vertical_job = {**base_job, "output_profile": "vertical", "aspect_ratio": "9:16"}
            api._write_brief_yaml(vertical_path, vertical_job)
            vertical_payload = yaml.safe_load(vertical_path.read_text())

            self.assertEqual(default_payload["output_profile"], "landscape")
            self.assertEqual(default_payload["aspect_ratio"], "16:9")
            self.assertEqual(vertical_payload["output_profile"], "vertical")
            self.assertEqual(vertical_payload["aspect_ratio"], "9:16")

    def test_dockerfile_fails_container_when_critical_process_exits(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        dockerignore = (ROOT / ".dockerignore").read_text()

        self.assertIn("wait -n", dockerfile)
        self.assertIn("ENV SOLO_STUDIO_REQUIRE_API_TOKEN=1", dockerfile)
        self.assertIn("nginx_pid=$!", dockerfile)
        self.assertIn("worker_processes 1;", dockerfile)
        self.assertIn("pid /tmp/nginx.pid;", dockerfile)
        self.assertIn("printf '%s\\n' \\", dockerfile)
        self.assertNotIn("printf '%s\\\\n' \\", dockerfile)
        self.assertIn("error_log /dev/stderr warn;", dockerfile)
        self.assertIn("access_log /dev/stdout;", dockerfile)
        self.assertEqual(dockerfile.count("client_body_temp_path /tmp/nginx-client;"), 1)
        self.assertEqual(dockerfile.count("proxy_temp_path /tmp/nginx-proxy;"), 1)
        self.assertEqual(dockerfile.count("access_log /dev/stdout;"), 1)
        self.assertIn("fastcgi_temp_path /tmp/nginx-fastcgi;", dockerfile)
        self.assertIn("uwsgi_temp_path /tmp/nginx-uwsgi;", dockerfile)
        self.assertIn("scgi_temp_path /tmp/nginx-scgi;", dockerfile)
        self.assertIn("nginx -t -c /etc/nginx/nginx.conf && \\", dockerfile)
        self.assertIn("rm -f /tmp/nginx.pid", dockerfile)
        self.assertIn('nginx -g "error_log /dev/stderr warn; daemon off;"', dockerfile)
        self.assertNotIn("sed -i -E", dockerfile)
        self.assertNotIn('nginx -g "error_log /dev/stderr warn; pid /tmp/nginx.pid; daemon off;"', dockerfile)
        self.assertIn("api_pid=$!", dockerfile)
        self.assertIn("worker_pid=$!", dockerfile)
        self.assertIn("if [ -n \"$worker_pid\" ]; then", dockerfile)
        self.assertIn("kill \"$nginx_pid\" \"$api_pid\" \"$worker_pid\"", dockerfile)
        self.assertIn("kill \"$nginx_pid\" \"$api_pid\"", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("/api/health", dockerfile)
        for sensitive_path in (".git/", ".solo_studio_api_token", "jobs.json", "output/", "tests/"):
            self.assertIn(sensitive_path, dockerignore)

    def test_dockerfile_normalizes_forwarded_client_ip_to_source_proxy(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertIn("proxy_set_header Host $host;", dockerfile)
        self.assertIn("proxy_set_header X-Real-IP $remote_addr;", dockerfile)
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", dockerfile)
        self.assertIn("proxy_set_header X-Forwarded-Proto $scheme;", dockerfile)
        self.assertNotIn("proxy_set_header X-Real-IP \\$remote_addr;", dockerfile)
        self.assertNotIn("proxy_set_header X-Forwarded-For \\$remote_addr;", dockerfile)
        self.assertNotIn("proxy_set_header X-Forwarded-Proto \\$scheme;", dockerfile)
        self.assertNotIn("proxy_set_header X-Forwarded-For $http_x_forwarded_for", dockerfile)

    def test_deploy_script_requires_token_and_smokes_protected_jobs_route(self):
        deploy = (ROOT / "deploy-traefik.sh").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertIn("SOLO_STUDIO_API_TOKEN_FILE", deploy)
        self.assertIn("-e SOLO_STUDIO_API_TOKEN_FILE=/run/secrets/solo_studio_api_token", deploy)
        self.assertIn('type=bind,src=$SOLO_STUDIO_API_TOKEN_FILE,dst=/run/secrets/solo_studio_api_token,ro', deploy)
        self.assertNotIn("-e SOLO_STUDIO_API_TOKEN \\\n", deploy)
        self.assertIn("-e SOLO_STUDIO_REQUIRE_API_TOKEN=1", deploy)
        self.assertIn("-e SOLO_STUDIO_TRUST_PROXY_HEADERS=1", deploy)
        self.assertIn("-e SOLO_STUDIO_TRUSTED_PROXY_NETWORKS=127.0.0.1/32,::1/128", deploy)
        self.assertIn("-e SOLO_STUDIO_SESSION_COOKIE_PATH=/video", deploy)
        self.assertIn("SOLO_STUDIO_CORS_ORIGINS", deploy)
        self.assertIn('case "${SOLO_STUDIO_ENABLE_HIGGSFIELD,,}"', deploy)
        self.assertIn("curl_config_dir=$(mktemp -d)", deploy)
        self.assertIn("chmod 700 \"$curl_config_dir\"", deploy)
        self.assertIn("os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)", deploy)
        self.assertIn("os.fstat(token_fd)", deploy)
        self.assertIn("os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)", deploy)
        self.assertIn('/run/secrets/solo_studio_api_token', deploy)
        self.assertIn('--config \"$token_config\"', deploy)
        self.assertNotIn('Authorization: Bearer $SOLO_STUDIO_API_TOKEN', deploy)
        self.assertNotIn('> \"$curl_config\"', deploy)
        self.assertIn("SOLO_STUDIO_CURL_CONNECT_TIMEOUT", deploy)
        self.assertIn("SOLO_STUDIO_CURL_MAX_TIME", deploy)
        self.assertIn("CURL_BOUNDED=(--connect-timeout", deploy)
        self.assertIn('curl "${CURL_BOUNDED[@]}" -fsS --config "$curl_config"', deploy)
        self.assertNotIn('curl -fsS -H "Authorization: Bearer ***', deploy)
        self.assertIn("https://edgescout.tech/video/api/jobs?limit=1", deploy)
        self.assertNotIn("SOLO_STUDIO_DISABLE_WORKER=1", deploy)
        self.assertIn("python runtime_init.py", dockerfile)

    def test_deploy_script_tags_named_rollback_image_before_live_replacement(self):
        deploy = (ROOT / "deploy-traefik.sh").read_text()

        self.assertIn("rollback_tag=\"solo-studio-video:rollback-", deploy)
        self.assertIn("docker tag \"$current_image\" \"$rollback_tag\"", deploy)
        self.assertIn("rollback_live", deploy)
        self.assertIn("preflight_image \"$rollback_tag\" rollback", deploy)
        self.assertIn("preflight_release_image", deploy)
        self.assertLess(deploy.index("preflight_release_image\n\ncurl_config"), deploy.index('remove_container_and_verify "$APP_NAME"', deploy.index("=== Deploying on edgescout.tech/video ===")))
        self.assertIn("SOLO_STUDIO_EXPECTED_RUNTIME_UID", deploy)
        self.assertIn("python -c \"import api, job_store, worker\"", deploy)
        self.assertIn('start_container_and_reconcile "$release_tag"', deploy)
        self.assertIn("container_replaced=0", deploy)
        self.assertIn("container_replaced=1", deploy)
        self.assertIn("Container was not replaced yet; leaving existing service untouched.", deploy)
        self.assertIn("LOCAL_HEALTH_OK attempt=$attempt", deploy)
        self.assertIn("Local container smoke failed after $attempt attempts", deploy)
        self.assertIn("wait_for_local_health", deploy)
        self.assertIn("wait_for_public_health", deploy)
        self.assertIn("return 1", deploy)
        self.assertIn("docker run -d", deploy)
        self.assertIn("$rollback_tag", deploy)
        self.assertNotIn("docker rm -f solo-studio-video 2>/dev/null || true\n\n# Ensure jobs.json", deploy)
        self.assertIn("for attempt in", deploy)
        self.assertIn("PUBLIC_HEALTH_OK", deploy)

    def test_provider_cli_is_pinned_and_real_mode_is_explicitly_configured(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        deploy = (ROOT / "deploy-traefik.sh").read_text()

        self.assertIn("@higgsfield/cli@${HIGGSFIELD_CLI_VERSION}", dockerfile)
        self.assertIn("ARG HIGGSFIELD_CLI_VERSION=1.1.23", dockerfile)
        self.assertIn("SOLO_STUDIO_ENABLE_HIGGSFIELD", deploy)
        self.assertIn("SOLO_STUDIO_HIGGSFIELD_MODEL", deploy)
        self.assertIn("HIGGSFIELD_CREDENTIALS_FILE", deploy)
        self.assertIn("credentials.json:ro", deploy)

    def test_package_status_passes_deadline_to_voiceover_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "audio"
            audio.mkdir()
            (audio / "voiceover.mp3").write_bytes(b"voiceover")
            seen = []

            def probe(path, *, deadline=None, **_kwargs):
                seen.append((Path(path).name, deadline))
                return {"valid": False}

            deadline = time.monotonic() + 60.0
            with patch("package_utils._probe_media", side_effect=probe):
                compute_package_status(root, deadline=deadline)

            self.assertIn(("voiceover.mp3", deadline), seen)
    def test_package_status_stops_when_deadline_is_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("package_utils._probe_media", side_effect=AssertionError("probe must not start after deadline")):
                summary = compute_package_status(root, deadline=0.0)
            self.assertFalse(summary["has_final_video"])
            self.assertFalse(summary["has_voiceover"])
            self.assertIn("deadline exceeded", " ".join(summary["artifact_errors"]))


if __name__ == "__main__":
    unittest.main()
