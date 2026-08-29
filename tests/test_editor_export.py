import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engines.editor_export import generate_fcpxml


class EditorExportTests(unittest.TestCase):
    def test_fcpxml_declares_every_reference_and_uses_exact_frame_duration(self):
        xml = generate_fcpxml(
            {
                "title": "Example",
                "total_duration": 3,
                "scenes": [
                    {"scene_number": 1, "duration_seconds": 1, "narration": "one"},
                    {"scene_number": 2, "duration_seconds": 2, "narration": "two"},
                ],
            },
            {"export": {"fps": 30}},
        )
        root = ElementTree.fromstring(xml)
        resources = root.find("resources")
        if resources is None:
            self.fail("FCPXML resources element is missing")
        resource_ids = {
            element.get("id")
            for element in resources
            if element.get("id")
        }
        refs = {
            element.get("ref")
            for element in root.iter()
            if element.get("ref")
        }
        self.assertTrue(refs <= resource_ids)
        self.assertIn("r1", resource_ids)
        format_element = resources.find("format")
        sequence = root.find(".//sequence")
        if format_element is None or sequence is None:
            self.fail("FCPXML format or sequence element is missing")
        self.assertEqual(format_element.get("frameDuration"), "1/30s")
        self.assertEqual(len(root.findall(".//sequence/spine")), 1)
        self.assertEqual(sequence.get("duration"), "3/1s")

    def test_fcpxml_rejects_invalid_fps_and_scene_duration(self):
        storyboard = {
            "total_duration": 1,
            "scenes": [{"scene_number": 1, "duration_seconds": 1}],
        }
        with self.assertRaises(ValueError):
            generate_fcpxml(storyboard, {"export": {"fps": 0}})
        with self.assertRaises(ValueError):
            generate_fcpxml(
                {**storyboard, "scenes": [{"scene_number": 1, "duration_seconds": float("nan")}]},
                {"export": {"fps": 30}},
            )

    def test_fcpxml_uses_vertical_profile_dimensions(self):
        xml = generate_fcpxml(
            {
                "output_profile": "vertical",
                "aspect_ratio": "9:16",
                "resolution": "1080x1920",
                "scenes": [{"scene_number": 1, "duration_seconds": 1}],
            },
            {"export": {"fps": 30}},
        )
        root = ElementTree.fromstring(xml)
        format_element = root.find("resources/format")
        if format_element is None:
            self.fail("FCPXML format is missing")
        self.assertEqual(format_element.get("width"), "1080")
        self.assertEqual(format_element.get("height"), "1920")

    def test_fcpxml_uses_accumulated_rounded_frames_and_contiguous_text_resources(self):
        xml = generate_fcpxml(
            {
                "scenes": [
                    {"scene_number": 1, "duration_seconds": 0.025, "narration": "one"},
                    {"scene_number": 2, "duration_seconds": 0.025, "narration": "two"},
                ]
            },
            {"export": {"fps": 30}},
        )
        root = ElementTree.fromstring(xml)
        sequence = root.find(".//sequence")
        if sequence is None:
            self.fail("FCPXML sequence is missing")
        self.assertEqual(sequence.get("duration"), "1/15s")
        text_refs = {
            element.get("ref")
            for element in root.findall(".//text-style")
            if element.get("ref")
        }
        resource_ids = {element.get("id") for element in root.findall("resources/*")}
        self.assertTrue(text_refs <= resource_ids)
        with self.assertRaises(ValueError):
            generate_fcpxml(
                {"scenes": [{"scene_number": 10, "duration_seconds": 1}]},
                {"export": {"fps": 30}},
            )
