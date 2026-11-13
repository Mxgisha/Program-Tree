"""Focused tests for Python scoping and import semantics."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from progmap.pipeline import build_graph


def build(files: dict[str, str]) -> tuple[dict, set[tuple[str, str, str]]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _graph, report = build_graph(root)
    edges = {(e["src"], e["dst"], e["kind"]) for e in report["edges"]}
    return report, edges


class ScopeTests(unittest.TestCase):
    def test_closure_sees_outer_local(self):
        _r, edges = build({
            "a.py": (
                "class Service:\n"
                "    def run(self):\n"
                "        return 1\n"
                "\n"
                "def outer():\n"
                "    svc = Service()\n"
                "    def inner():\n"
                "        return svc.run()\n"
                "    return inner\n"
            ),
        })
        self.assertIn(("a::outer.inner", "a::Service.run", "calls"), edges)

    def test_shadowed_builtin_resolves_to_local_definition(self):
        _r, edges = build({
            "a.py": (
                "def len(value):\n"
                "    return 0\n"
                "\n"
                "def f(items):\n"
                "    return len(items)\n"
            ),
        })
        self.assertIn(("a::f", "a::len", "calls"), edges)

    def test_star_import_fallback(self):
        _r, edges = build({
            "a.py": "from b import *\n\n\ndef f():\n    return helper()\n",
            "b.py": "def helper():\n    return 1\n",
        })
        self.assertIn(("a::f", "b::helper", "calls"), edges)

    def test_module_attribute_chain_with_instantiation(self):
        _r, edges = build({
            "a.py": (
                "import b\n"
                "\n"
                "def f():\n"
                "    return b.Obj().go()\n"
            ),
            "b.py": (
                "class Obj:\n"
                "    def go(self):\n"
                "        return 1\n"
            ),
        })
        self.assertIn(("a::f", "b::Obj", "instantiates"), edges)
        self.assertIn(("a::f", "b::Obj.go", "calls"), edges)

    def test_nested_class_method(self):
        _r, edges = build({
            "a.py": (
                "class Outer:\n"
                "    class Inner:\n"
                "        def ping(self):\n"
                "            return 1\n"
                "\n"
                "    def use(self):\n"
                "        item = self.Inner()\n"
                "        return item.ping()\n"
            ),
        })
        self.assertIn(("a::Outer.use", "a::Outer.Inner", "instantiates"), edges)
        self.assertIn(("a::Outer.use", "a::Outer.Inner.ping", "calls"), edges)

    def test_inherited_attribute_via_base(self):
        _r, edges = build({
            "a.py": (
                "class Base:\n"
                "    def shared(self):\n"
                "        return 1\n"
                "\n"
                "class Child(Base):\n"
                "    def use(self):\n"
                "        return self.shared()\n"
            ),
        })
        self.assertIn(("a::Child.use", "a::Base.shared", "calls"), edges)

    def test_decorator_edge_direction(self):
        _r, edges = build({
            "a.py": (
                "def deco(fn):\n"
                "    return fn\n"
                "\n"
                "@deco\n"
                "def handler():\n"
                "    return 1\n"
            ),
        })
        self.assertIn(("a::deco", "a::handler", "decorates"), edges)

    def test_self_attribute_annotation(self):
        _r, edges = build({
            "a.py": (
                "class Repo:\n"
                "    def get(self):\n"
                "        return 1\n"
                "\n"
                "class Svc:\n"
                "    def __init__(self):\n"
                "        self.repo: Repo = Repo()\n"
                "\n"
                "    def load(self):\n"
                "        return self.repo.get()\n"
            ),
        })
        self.assertIn(("a::Svc.load", "a::Repo.get", "calls"), edges)


if __name__ == "__main__":
    unittest.main()
