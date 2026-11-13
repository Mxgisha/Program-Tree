"""CLI smoke tests: output files, roots, config."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from progmap.cli import main


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


class CliTests(unittest.TestCase):
    def test_writes_all_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            root.mkdir()
            write(root / "app.py", "def used():\n    return 1\n\n\ndef go():\n    return used()\n")
            out = Path(tmp) / "out"
            code = main([str(root), "-o", str(out), "--no-git"])
            self.assertEqual(code, 0)
            for name in ("map.json", "explorer.html", "report.md", "graph.dot"):
                self.assertTrue((out / name).exists(), name)
            report = json.loads((out / "map.json").read_text())
            self.assertIn("dead_code", report)
            self.assertIn("git", report)
            self.assertEqual(report["git"]["reason"], "disabled")
            html = (out / "explorer.html").read_text()
            self.assertIn('id="progmap-data"', html)
            self.assertNotIn("__PROGMAP_DATA__", html)

    def test_config_roots_are_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            write(root / "app.py", "def _dead():\n    return 1\n")
            out = Path(tmp) / "out"
            main([str(root), "-o", str(out), "--no-git"])
            report = json.loads((out / "map.json").read_text())
            self.assertIn("app::_dead", {c["id"] for c in report["dead_code"]["candidates"]})

            write(root / ".progmap.json", '{"roots": ["app::_dead"]}')
            main([str(root), "-o", str(out), "--no-git"])
            report = json.loads((out / "map.json").read_text())
            self.assertNotIn("app::_dead", {c["id"] for c in report["dead_code"]["candidates"]})

    def test_cli_root_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            write(root / "app.py", "def _dead():\n    return 1\n")
            out = Path(tmp) / "out"
            main([str(root), "-o", str(out), "--no-git", "--root", "app::_dead"])
            report = json.loads((out / "map.json").read_text())
            self.assertNotIn("app::_dead", {c["id"] for c in report["dead_code"]["candidates"]})


if __name__ == "__main__":
    unittest.main()
