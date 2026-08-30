import os
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engines import source_ingest_agent
import package_utils


class UnsupportedMediaResponse:
    status = 200
    headers = {"Content-Type": "application/pdf"}

    def read(self, size=-1):
        return b"%PDF-1.7"

    def close(self):
        return None


class UnsupportedMediaOpener:
    def open(self, request, timeout=None):
        return UnsupportedMediaResponse()


class SourceMediaValidationTests(unittest.TestCase):
    def test_non_text_content_is_rejected_before_body_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(source_ingest_agent.SourceIngestError) as raised:
                source_ingest_agent.ingest_sources(
                    ["https://example.com/document"],
                    Path(tmp),
                    opener=UnsupportedMediaOpener(),
                    resolver=lambda host: ["93.184.216.34"],
                )
        self.assertEqual(raised.exception.code, "unsupported_media")
    def test_malformed_percent_escape_is_rejected_without_network(self):
        for value in ("https://example.com/%", "https://example.com/%2", "https://example.com/%zz"):
            with self.subTest(value=value):
                with self.assertRaises(source_ingest_agent.SourceIngestError) as raised:
                    source_ingest_agent.validate_url_syntax(value)
                self.assertEqual(raised.exception.code, "invalid_url")

    def test_existing_symlink_ancestor_is_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_parent = root / "real"
            real_parent.mkdir()
            link_parent = root / "link"
            link_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaises(source_ingest_agent.SourceIngestError) as raised:
                source_ingest_agent.ingest_sources(
                    ["https://example.com/document"],
                    link_parent / "child",
                    opener=UnsupportedMediaOpener(),
                    resolver=lambda host: ["93.184.216.34"],
                )
        self.assertEqual(raised.exception.code, "write_error")

    def test_existing_output_root_replacement_before_open_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            root.mkdir()
            real_open_directory = source_ingest_agent._open_directory_no_follow
            replaced = False

            def replace_before_open(path, *, create):
                nonlocal replaced
                if Path(path) == root and not replaced:
                    replaced = True
                    root.rename(Path(tmp) / "detached")
                    root.mkdir()
                return real_open_directory(path, create=create)

            with patch.object(source_ingest_agent, "_open_directory_no_follow", side_effect=replace_before_open):
                with self.assertRaises(source_ingest_agent.SourceIngestError) as raised:
                    source_ingest_agent.ingest_sources(
                        ["https://example.com/document"],
                        root,
                        resolver=lambda host: ["93.184.216.34"],
                    )
        self.assertEqual(raised.exception.code, "write_error")


class DescriptorFailureCleanupTests(unittest.TestCase):
    def _assert_fstat_failure_closes_descriptor(self, opener):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            target = root / "artifact.txt"
            target.write_text("safe", encoding="utf-8")
            real_open = package_utils.os.open
            real_fstat = package_utils.os.fstat
            target_fd = -1
            fail_once = True

            def capture_open(path, *args, **kwargs):
                nonlocal target_fd
                descriptor = real_open(path, *args, **kwargs)
                if path == "artifact.txt":
                    target_fd = descriptor
                return descriptor

            def fail_target_fstat(descriptor):
                nonlocal fail_once
                if descriptor == target_fd and fail_once:
                    fail_once = False
                    raise OSError("injected fstat failure")
                return real_fstat(descriptor)

            before = len(os.listdir("/proc/self/fd"))
            with patch.object(package_utils.os, "open", side_effect=capture_open), patch.object(
                package_utils.os, "fstat", side_effect=fail_target_fstat
            ):
                with self.assertRaises(OSError):
                    opener(root, target)
            after = len(os.listdir("/proc/self/fd"))
            self.assertEqual(after, before)

    def test_regular_descriptor_fstat_failure_closes_descriptor(self):
        self._assert_fstat_failure_closes_descriptor(lambda root, target: package_utils._open_regular_descriptor(target))

    def test_rooted_descriptor_fstat_failure_closes_descriptor(self):
        self._assert_fstat_failure_closes_descriptor(package_utils._open_regular_under_root)


class AtomicRollbackIntegrityTests(unittest.TestCase):
    def test_same_size_backup_mutation_never_restores_attacker_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.txt"
            path.write_text("ORIGINAL", encoding="utf-8")
            real_fsync = package_utils.os.fsync
            state = {"mutated": False}

            def fsync(descriptor):
                real_fsync(descriptor)
                if not state["mutated"]:
                    try:
                        target = os.readlink(f"/proc/self/fd/{descriptor}")
                    except OSError:
                        return
                    if ".backup" in target:
                        os.pwrite(descriptor, b"MUTATION", 0)
                        real_fsync(descriptor)
                        state["mutated"] = True

            real_read = package_utils._read_bounded_utf8

            def fail_published(descriptor, **kwargs):
                target = os.readlink(f"/proc/self/fd/{descriptor}")
                if target.endswith("manifest.txt"):
                    raise OSError("injected readback failure")
                return real_read(descriptor, **kwargs)

            with patch.object(package_utils.os, "fsync", side_effect=fsync), patch.object(
                package_utils, "_read_bounded_utf8", side_effect=fail_published
            ):
                with self.assertRaises(OSError):
                    package_utils.atomic_write_text(path, "NEW")

            destination = path.read_text(encoding="utf-8") if path.exists() else None
            self.assertTrue(state["mutated"])
            self.assertIn(destination, (None, "ORIGINAL"))

    def test_valid_fallback_rollback_restores_original_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.txt"
            path.write_text("ORIGINAL", encoding="utf-8")
            real_read = package_utils._read_bounded_utf8

            def fail_after_removing_displaced(descriptor, **kwargs):
                target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
                if target == path:
                    for staging in Path(tmp).glob(".staging-*"):
                        for temporary in staging.glob("*.tmp"):
                            temporary.unlink()
                    raise OSError("injected readback failure")
                return real_read(descriptor, **kwargs)

            with patch.object(
                package_utils, "_read_bounded_utf8", side_effect=fail_after_removing_displaced
            ):
                with self.assertRaises(OSError):
                    package_utils.atomic_write_text(path, "NEW")

            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), "ORIGINAL")


if __name__ == "__main__":
    unittest.main()
