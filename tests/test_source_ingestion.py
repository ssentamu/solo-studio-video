import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import api
import pipeline
from engines import source_ingest_agent


class FakeResponse:
    def __init__(self, body=b"<html><title>Example</title><p>Hello</p></html>", status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.read_sizes = []

    def getcode(self):
        return self.status

    def geturl(self):
        return "https://example.com/"

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size is None or size < 0:
            chunk, self.body = self.body, b""
            return chunk
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def close(self):
        return None


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("unexpected second request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class SourceIngestionTests(unittest.TestCase):
    def test_url_validation_accepts_public_url_through_resolver_seam(self):
        validated = source_ingest_agent.validate_url(
            "https://example.com/articles/start?x=1",
            resolver=lambda host: ["93.184.216.34"],
        )
        self.assertEqual(validated.hostname, "example.com")

    def test_url_validation_rejects_private_and_unsafe_forms(self):
        invalid = (
            "http://localhost/",
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://[fd00::1]/",
            "http://[::ffff:127.0.0.1]/",
            "https://user:pass@example.com/",
            "https://example.com:8443/",
            "https://example.com/#fragment",
            "file:///tmp/example",
            "https:///missing-host",
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(source_ingest_agent.SourceIngestError):
                    source_ingest_agent.validate_url(url, resolver=lambda host: ["93.184.216.34"])

    def test_redirect_private_target_is_rejected_without_second_request(self):
        opener = FakeOpener([
            FakeResponse(status=302, headers={"Location": "http://127.0.0.1/private"}),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(source_ingest_agent.SourceIngestError) as raised:
                source_ingest_agent.ingest_sources(
                    ["https://example.com/start"],
                    Path(tmp),
                    opener=opener,
                    resolver=lambda host: ["93.184.216.34"],
                )
        self.assertEqual(raised.exception.code, "blocked_address")
        self.assertEqual(len(opener.requests), 1)

    def test_oversized_body_is_rejected_while_reading_in_bounded_chunks(self):
        response = FakeResponse(body=b"123456")
        opener = FakeOpener([response])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(source_ingest_agent.SourceIngestError) as raised:
                source_ingest_agent.ingest_sources(
                    ["https://example.com/"],
                    Path(tmp),
                    opener=opener,
                    resolver=lambda host: ["93.184.216.34"],
                    max_body_bytes=5,
                    chunk_size=2,
                )
        self.assertEqual(raised.exception.code, "body_too_large")
        self.assertTrue(response.read_sizes)
        self.assertTrue(all(size <= 2 for size in response.read_sizes))

    def test_extraction_is_deterministic_bounded_and_omits_script_style(self):
        html = """
        <html><head><title>  A &amp; B </title><meta name="description" content="Summary"></head>
        <body><script>secret()</script><style>.secret{}</style><h1>Heading</h1>
        <p>Paragraph one.</p><p>""" + ("long " * 1000) + "</p></body></html>"
        first = source_ingest_agent.extract_document(html, max_text_chars=120)
        second = source_ingest_agent.extract_document(html, max_text_chars=120)
        self.assertEqual(first, second)
        self.assertEqual(first["title"], "A & B")
        self.assertEqual(first["meta_description"], "Summary")
        self.assertEqual(first["headings"], ["Heading"])
        self.assertNotIn("secret", json.dumps(first))
        self.assertLessEqual(len(first["text"]), 120)

    def test_network_timeout_and_malformed_failures_use_safe_classifications(self):
        for failure, code in ((TimeoutError(), "timeout"), (OSError(), "network_error")):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(source_ingest_agent.SourceIngestError) as raised:
                    source_ingest_agent.ingest_sources(
                        ["https://example.com/"], Path(tmp),
                        opener=FakeOpener([failure]),
                        resolver=lambda host: ["93.184.216.34"],
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("example.com", str(raised.exception))

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(source_ingest_agent.SourceIngestError) as raised:
                source_ingest_agent.ingest_sources(
                    ["https://example.com/"], Path(tmp),
                    opener=FakeOpener([FakeResponse(body=b"\xff")]),
                    resolver=lambda host: ["93.184.216.34"],
                )
            self.assertEqual(raised.exception.code, "malformed_response")

    def test_ingestion_writes_provenance_and_research_consumes_evidence(self):
        response = FakeResponse(body=b"<html><title>Grounded topic</title><p>Evidence from source.</p></html>")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest = source_ingest_agent.ingest_sources(
                ["https://example.com/source"], output,
                opener=FakeOpener([response]),
                resolver=lambda host: ["93.184.216.34"],
            )
            self.assertEqual(manifest["sources"][0]["source_url"], "https://example.com/source")
            self.assertTrue((output / "source_context.md").is_file())
            self.assertTrue((output / "reverse_brief.json").is_file())
            from engines.research_agent import generate_brief
            brief = generate_brief(
                {"topic": "demo", "reference_urls": ["https://example.com/source"]},
                source_context=(output / "source_context.md").read_text(encoding="utf-8"),
                reverse_brief=json.loads((output / "reverse_brief.json").read_text(encoding="utf-8")),
            )
            saved = json.loads(json.dumps(brief.__dict__))
            self.assertEqual(saved["reference_urls"], ["https://example.com/source"])
            self.assertIn("Evidence from source.", saved["source_context"])
            self.assertTrue(saved["reverse_brief"]["heuristic"])


class SourceApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.original = {
            "DATABASE_CONFIGURED": api.DATABASE_CONFIGURED,
            "DATABASE_FILE": api.DATABASE_FILE,
            "JOBS_FILE": api.JOBS_FILE,
            "OUTPUT_ROOT": api.OUTPUT_ROOT,
            "API_TOKEN": api.API_TOKEN,
            "REQUIRE_API_TOKEN": api.REQUIRE_API_TOKEN,
        }
        api.DATABASE_CONFIGURED = True
        api.DATABASE_FILE = root / "state" / "solo.sqlite3"
        api.JOBS_FILE = root / "jobs.json"
        api.OUTPUT_ROOT = root / "output"
        api.API_TOKEN = ""
        api.REQUIRE_API_TOKEN = False
        api.OUTPUT_ROOT.mkdir(parents=True)
        import job_store
        job_store.initialize(api.DATABASE_FILE)
        self.client = TestClient(api.app)

    def tearDown(self):
        for key, value in self.original.items():
            setattr(api, key, value)
        self.tmp.cleanup()

    def test_api_accepts_one_url_without_dns_and_persists_it_to_brief_yaml(self):
        with patch.object(source_ingest_agent, "resolve_hostname", side_effect=AssertionError("API must not resolve DNS")):
            response = self.client.post("/api/jobs", json={"topic": "URLs", "reference_urls": ["https://example.com/source"]})
        self.assertEqual(response.status_code, 201)
        job_id = response.json()["id"]
        import yaml
        saved = yaml.safe_load((api.OUTPUT_ROOT / job_id / "brief.yaml").read_text(encoding="utf-8"))
        self.assertEqual(saved["reference_urls"], ["https://example.com/source"])
        self.assertEqual(response.json()["reference_urls"], ["https://example.com/source"])

    def test_api_rejects_more_than_three_or_syntactically_invalid_urls(self):
        for urls in (
            ["https://example.com/1", "https://example.com/2", "https://example.com/3", "https://example.com/4"],
            ["http://localhost/private"],
            ["https://user:pass@example.com/"],
        ):
            with self.subTest(urls=urls):
                response = self.client.post("/api/jobs", json={"topic": "URLs", "reference_urls": urls})
                self.assertEqual(response.status_code, 422)


class PipelineSourceOrderingTests(unittest.TestCase):
    def test_pipeline_ingests_sources_before_research_only_when_references_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief = root / "brief.yaml"
            brief.write_text("topic: demo\nreference_urls:\n  - https://example.com/source\n", encoding="utf-8")
            events = []
            def fake_stage(name, *args):
                events.append(("stage", name))
                if name == "4. Production Agent":
                    Path(args[-1], "video_prompts.json").write_text("{}", encoding="utf-8")
                return True

            with patch.object(
                pipeline,
                "run_stage",
                side_effect=fake_stage,
            ), patch.object(pipeline, "_generate_thumbnail_prompt"), patch.object(
                pipeline, "_assemble_verified_output", return_value=None
            ), patch.object(
                pipeline, "write_package_manifest", return_value={"package_status": "completed"}
            ), patch.object(
                pipeline, "read_json_artifact", return_value={"scenes": [], "output_profile": "landscape", "aspect_ratio": "16:9"}
            ):
                old_argv = sys.argv[:]
                sys.argv = ["pipeline.py", str(brief), "-o", str(root / "out"), "--skip-visuals"]
                try:
                    pipeline.main()
                finally:
                    sys.argv = old_argv
            self.assertLess(
                events.index(("stage", "0. Source Ingestion")),
                events.index(("stage", "1. Research Agent")),
            )

            events.clear()
            brief.write_text("topic: demo\n", encoding="utf-8")
            with patch.object(pipeline, "run_stage", return_value=False) as stage:
                old_argv = sys.argv[:]
                sys.argv = ["pipeline.py", str(brief), "-o", str(root / "out2"), "--skip-visuals"]
                try:
                    with self.assertRaises(SystemExit):
                        pipeline.main()
                finally:
                    sys.argv = old_argv
            self.assertEqual(stage.call_args.args[0], "1. Research Agent")


if __name__ == "__main__":
    unittest.main()
