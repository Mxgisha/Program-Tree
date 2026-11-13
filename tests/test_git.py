"""Tests for git history signals (skipped when git is unavailable)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from progmap.gitsignals import analyze
from progmap.pipeline import build_graph

GIT = shutil.which("git")


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True)


@unittest.skipUnless(GIT, "git not installed")
class GitSignalTests(unittest.TestCase):
    def _repo(self, root: Path) -> dict[str, str]:
        files = {
            "a.py": "def fa():\n    return 1\n",
            "b.py": "def fb():\n    return 2\n",
            "c.py": "def fc():\n    return 3\n",
        }
        git(root, "init", "-q")
        git(root, "config", "user.email", "t@example.com")
        git(root, "config", "user.name", "t")
        for name, text in files.items():
            (root / name).write_text(text)
        git(root, "add", "-A")
        git(root, "commit", "-qm", "init")
        for i, changed in enumerate((["a.py", "b.py"], ["a.py", "b.py"],
                                     ["a.py", "c.py"], ["a.py", "b.py"])):
            for name in changed:
                (root / name).write_text(files[name] + f"\n# {i}\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", f"c{i}")
        return files

    def test_cochange_and_churn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root)
            graph, _report = build_graph(root, git=False)
            signals = analyze(root, graph)
            self.assertTrue(signals["available"])
            self.assertEqual(signals["commits"], 5)
            pairs = {(r["a"], r["b"]): r for r in signals["cochange"]}
            self.assertIn(("a", "b"), pairs)
            self.assertEqual(pairs[("a", "b")]["count"], 4)
            self.assertEqual(signals["churn"]["a"], 5)
            self.assertEqual(signals["churn"]["c"], 2)
            self.assertTrue(signals["hotspots"])
            self.assertEqual(signals["hotspots"][0]["module"], "a")

    def test_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root)
            _graph, report = build_graph(root, git=False)
            self.assertFalse(report["git"]["available"])
            self.assertEqual(report["git"]["reason"], "disabled")

    def test_pipeline_includes_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root)
            _graph, report = build_graph(root)
            self.assertTrue(report["git"]["available"])
            self.assertEqual(report["git"]["commits"], 5)

    def test_non_repo_is_graceful(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("def f():\n    return 1\n")
            graph, _report = build_graph(root, git=False)
            signals = analyze(root, graph)
            self.assertFalse(signals["available"])


if __name__ == "__main__":
    unittest.main()
