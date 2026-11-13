"""End-to-end tests for the progmap engine."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from progmap.discovery import module_name_for
from progmap.pipeline import build_graph

FIXTURES = Path(__file__).parent / "fixtures" / "demo"


def edge_set(report: dict) -> set[tuple[str, str, str]]:
    return {(e["src"], e["dst"], e["kind"]) for e in report["edges"]}


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph, cls.report = build_graph(FIXTURES)
        cls.edges = edge_set(cls.report)
        cls.symbols = {s["id"]: s for s in cls.report["symbols"]}

    def test_no_parse_errors(self):
        self.assertEqual(self.report["meta"]["errors"], [])

    def test_counts(self):
        stats = self.report["stats"]
        self.assertEqual(stats["files"], 6)
        self.assertGreater(stats["symbols"], 30)
        self.assertGreater(stats["edges"], 20)

    def test_call_through_typed_parameter(self):
        # user: User -> user.describe() resolves to the inherited Base.describe
        self.assertIn(("app.services::UserService.describe",
                       "app.models::Base.describe", "calls"), self.edges)

    def test_self_attribute_from_constructor_parameter(self):
        # self.repo = repo (repo: Repository); self.repo.add(...) must resolve
        self.assertIn(("app.services::UserService.create",
                       "app.services::Repository.add", "calls"), self.edges)
        self.assertIn(("app.services::UserService.find",
                       "app.services::Repository.get", "calls"), self.edges)

    def test_inheritance_and_inherited_method(self):
        self.assertIn(("app.models::User", "app.models::Base", "inherits"), self.edges)
        self.assertIn(("app.models::Admin", "app.models::User", "inherits"), self.edges)
        self.assertIn(("app.models::Base.describe", "app.models::Base.name", "calls"),
                      self.edges)

    def test_super_call_resolves_to_nearest_base(self):
        # super().name() inside Admin.label must hit User.name, not Base.name
        self.assertIn(("app.models::Admin.label", "app.models::User.name", "calls"),
                      self.edges)

    def test_aliased_import_resolves(self):
        self.assertIn(("app.services::UserService.describe",
                       "app.util::slugify", "calls"), self.edges)

    def test_module_import_edges(self):
        self.assertIn(("app.services", "app.models", "imports"), self.edges)
        self.assertIn(("app.services", "app.util", "imports"), self.edges)
        self.assertIn(("app", "app.models", "imports"), self.edges)

    def test_instantiations(self):
        self.assertIn(("app.services::UserService.create",
                       "app.models::User", "instantiates"), self.edges)

    def test_package_reexport_resolves(self):
        # ``from app import User`` hits the name re-exported by app/__init__.py
        self.assertIn(("app.cli::make", "app.models::User", "instantiates"), self.edges)
        # ``from app import util as u`` then ``u.slugify``
        self.assertIn(("app.cli::render", "app.util::slugify", "calls"), self.edges)
        # method call through a returned instance
        self.assertIn(("app.cli::run", "app.services::UserService.create", "calls"),
                      self.edges)

    def test_callable_object_dispatch(self):
        self.assertIn(("app.util::scale", "app.util::Multiplier.__call__", "calls"),
                      self.edges)

    def test_callback_reference_not_call(self):
        # callback = self.repo.get is a reference, not a call
        self.assertIn(("app.services::UserService.count",
                       "app.services::Repository.get", "references"), self.edges)

    def test_no_unresolved_names(self):
        self.assertEqual(self.report["unresolved"], [])

    def test_scope_shadowing(self):
        # User.name returns self.username; it must not link to a module-level name
        self.assertIn(("app.models::User.name", "app.models::User.username",
                       "references"), self.edges)

    def test_json_safe(self):
        import json
        json.dumps(self.report)


class CycleTests(unittest.TestCase):
    def test_import_and_call_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text(
                "import b\n\n\ndef fa():\n    return b.fb()\n")
            (root / "b.py").write_text(
                "import a\n\n\ndef fb():\n    return a.fa()\n")
            _graph, report = build_graph(root)
            module_cycles = report["cycles"]["modules"]
            self.assertTrue(any(set(c) == {"a", "b"} for c in module_cycles))
            function_cycles = report["cycles"]["functions"]
            self.assertTrue(any(set(c) == {"a::fa", "b::fb"} for c in function_cycles))

    def test_type_checking_import_is_not_a_runtime_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text(
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    import b\n"
                "\n\n"
                "def fa():\n"
                "    return 1\n")
            (root / "b.py").write_text(
                "import a\n\n\ndef fb():\n    return a.fa()\n")
            _graph, report = build_graph(root)
            self.assertEqual(report["cycles"]["modules"], [])
            type_edges = [e for e in report["module_edges"] if e.get("type_only")]
            self.assertTrue(any(e["src"] == "a" and e["dst"] == "b" for e in type_edges))

    def test_recursive_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r.py").write_text("def f(n):\n    if n:\n        return f(n - 1)\n")
            _graph, report = build_graph(root)
            self.assertIn(("r::f", "r::f", "calls"), edge_set(report))


class SrcLayoutTests(unittest.TestCase):
    def test_src_layout_relative_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "src" / "pkg"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("")
            (pkg / "a.py").write_text(
                "from .b import thing\n\n\ndef use():\n    return thing()\n")
            (pkg / "b.py").write_text("def thing():\n    return 1\n")
            _graph, report = build_graph(root)
            edges = edge_set(report)
            self.assertIn(("pkg.a", "pkg.b", "imports"), edges)
            self.assertIn(("pkg.a::use", "pkg.b::thing", "calls"), edges)
            names = {m["name"] for m in report["modules"]}
            self.assertEqual(names, {"pkg", "pkg.a", "pkg.b"})


class NamingTests(unittest.TestCase):
    def test_package_root_prefixes_modules(self):
        root = Path("/tmp/pkgroot")
        self.assertEqual(
            module_name_for(root / "core.py", root, set(), "click")[0], "click.core")

    def test_src_layout_strips_src(self):
        root = Path("/tmp/proj")
        name, _is_pkg, package = module_name_for(
            root / "src" / "pkg" / "mod.py", root, {"src"})
        self.assertEqual(name, "pkg.mod")
        self.assertEqual(package, "pkg")

    def test_init_becomes_package(self):
        root = Path("/tmp/proj")
        name, is_pkg, package = module_name_for(
            root / "pkg" / "__init__.py", root, set())
        self.assertEqual((name, is_pkg, package), ("pkg", True, "pkg"))


class SerializationTests(unittest.TestCase):
    def test_compact_symbol_dict_keeps_required_fields(self):
        _graph, report = build_graph(FIXTURES)
        for sym in report["symbols"]:
            self.assertIn("id", sym)
            self.assertIn("kind", sym)
            self.assertIn("line", sym)


if __name__ == "__main__":
    unittest.main()
