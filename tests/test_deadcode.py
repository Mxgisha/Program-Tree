"""Tests for root-set reachability and unused-code evidence."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from progmap.pipeline import build_graph

FIXTURES = Path(__file__).parent / "fixtures" / "demo"


def analyze(root: Path, **kwargs):
    return build_graph(root, **kwargs)[1]["dead_code"]


def write_project(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dead = analyze(FIXTURES)

    def test_reports_private_dead_function(self):
        ids = {c["id"] for c in self.dead["candidates"]}
        self.assertIn("app.util::unused_helper", ids)

    def test_dead_module_detected_and_symbols_collapsed(self):
        names = {m["name"] for m in self.dead["dead_modules"]}
        self.assertEqual(names, {"app.cli"})
        ids = {c["id"] for c in self.dead["candidates"]}
        self.assertFalse(any(i.startswith("app.cli::") for i in ids))

    def test_entry_module_is_live(self):
        self.assertNotIn("app.__main__", {m["name"] for m in self.dead["dead_modules"]})
        ids = {c["id"] for c in self.dead["candidates"]}
        self.assertNotIn("app.services::build", ids)

    def test_explicit_root_resurrects_module(self):
        dead = analyze(FIXTURES, roots=["app.cli::run"])
        self.assertEqual(dead["dead_modules"], [])
        ids = {c["id"] for c in dead["candidates"]}
        self.assertNotIn("app.cli::run", ids)

    def test_public_roots_can_hide_public_dead_code(self):
        dead = analyze(FIXTURES, root_public=True)
        ids = {c["id"] for c in dead["candidates"]}
        self.assertNotIn("app.util::unused_helper", ids)

    def test_reasons_and_confidence_are_present(self):
        for cand in self.dead["candidates"]:
            self.assertIn(cand["confidence"], ("high", "medium", "low"))
            self.assertTrue(cand["reason"])


class EvidenceTests(unittest.TestCase):
    def test_private_unused_is_high_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project(root, {
                "app.py": "def used():\n    return 1\n\n\ndef _dead():\n    return 2\n",
                "main.py": "from app import used\n\n\ndef go():\n    return used()\n",
            })
            dead = analyze(root)
            by_id = {c["id"]: c for c in dead["candidates"]}
            self.assertIn("app::_dead", by_id)
            self.assertEqual(by_id["app::_dead"]["confidence"], "high")
            self.assertIn("no callers", by_id["app::_dead"]["reason"])

    def test_only_dead_callers_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project(root, {
                "app.py": (
                    "def _leaf():\n    return 1\n"
                    "\n\ndef _middle():\n    return _leaf()\n"
                    "\n\ndef start():\n    return 1\n"
                ),
                "main.py": "from app import start\n\n\ndef go():\n    return start()\n",
            })
            dead = analyze(root)
            ids = {c["id"]: c for c in dead["candidates"]}
            self.assertIn("app::_middle", ids)
            self.assertIn("app::_leaf", ids)
            self.assertIn("dead code", ids["app::_leaf"]["reason"])
            self.assertTrue(ids["app::_leaf"]["callers"])

    def test_test_only_code_is_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project(root, {
                "app.py": "def _helper():\n    return 1\n",
                "tests/test_app.py": (
                    "import app\n\n\ndef test_helper():\n    assert app._helper()\n"
                ),
            })
            dead = analyze(root)
            self.assertNotIn("app::_helper", {c["id"] for c in dead["candidates"]})
            self.assertIn("app::_helper", {t["id"] for t in dead["test_only"]})

    def test_framework_decorator_is_a_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project(root, {
                "app.py": (
                    "def command(fn):\n    return fn\n"
                    "\n\n@command\n"
                    "def handler():\n    return 1\n"
                ),
            })
            dead = analyze(root)
            self.assertNotIn("app::handler", {c["id"] for c in dead["candidates"]})

    def test_extra_framework_decorator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project(root, {
                "app.py": (
                    "def custom(fn):\n    return fn\n"
                    "\n\n@custom\n"
                    "def _handler():\n    return 1\n"
                ),
            })
            self.assertIn("app::_handler", {c["id"] for c in analyze(root)["candidates"]})
            dead = analyze(root, framework_decorators=["custom"])
            self.assertNotIn("app::_handler", {c["id"] for c in dead["candidates"]})

    @unittest.skipUnless(sys.version_info >= (3, 11), "tomllib required")
    def test_pyproject_script_is_a_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project(root, {
                "pyproject.toml": (
                    '[project]\nname = "x"\nversion = "0"\n'
                    '[project.scripts]\nrun = "app:main"\n'
                ),
                "app.py": "def main():\n    return 0\n",
            })
            dead = analyze(root)
            self.assertNotIn("app::main", {c["id"] for c in dead["candidates"]})


if __name__ == "__main__":
    unittest.main()
