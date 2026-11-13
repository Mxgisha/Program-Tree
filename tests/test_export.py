"""Tests for the export layer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from progmap.export.dot import render_dot
from progmap.export.html import TEMPLATE, render_html
from progmap.export.report import render_report
from progmap.pipeline import build_graph

FIXTURES = Path(__file__).parent / "fixtures" / "demo"


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _graph, cls.report = build_graph(FIXTURES)

    def test_report_sections(self):
        text = render_report(self.report)
        self.assertIn("# progmap:", text)
        self.assertIn("## Most-called functions", text)
        self.assertIn("## Import cycles", text)

    def test_dot_is_valid_shape(self):
        dot = render_dot(self.report)
        self.assertTrue(dot.startswith("digraph progmap {"))
        self.assertTrue(dot.rstrip().endswith("}"))
        self.assertIn("->", dot)
        self.assertLessEqual(dot.count("->"), len(self.report["edges"]))

    def test_dot_limit(self):
        dot = render_dot(self.report, limit=5)
        self.assertLessEqual(dot.count("label="), 5 + 5)  # nodes + cluster labels

    def test_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            path.write_text(json.dumps(self.report), encoding="utf-8")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["stats"]["files"], 6)

    @unittest.skipUnless(TEMPLATE.exists(), "explorer template not built yet")
    def test_html_embeds_data(self):
        page = render_html(self.report, "demo")
        self.assertIn('id="progmap-data"', page)
        self.assertNotIn("__PROGMAP_DATA__", page)
        self.assertNotIn("__PROGMAP_TITLE__", page)
        self.assertIn("demo", page)


if __name__ == "__main__":
    unittest.main()
