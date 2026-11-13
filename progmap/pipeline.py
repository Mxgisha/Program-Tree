"""Orchestration: discover -> parse -> index -> bind -> resolve -> analyze."""

from __future__ import annotations

import ast
import tokenize
import time
from pathlib import Path

from . import __version__
from .analysis import analyze
from .binder import build_namespaces
from .deadcode import analyze as analyze_dead_code
from .discovery import prepare
from .gitsignals import analyze as analyze_git
from .indexer import index_module, is_test_path
from .model import Graph, ModuleInfo, Symbol
from .resolver import Resolver


def build_graph(root: Path, excludes: list[str] | None = None,
                roots: list[str] | None = None,
                root_public: bool | None = None,
                include_test_roots: bool = True,
                git: bool = True, git_commits: int = 1500,
                framework_decorators: list[str] | None = None) -> tuple[Graph, dict]:
    started = time.time()
    graph = Graph()
    files, info = prepare(root, excludes)
    modules: dict[str, ModuleInfo] = {}

    for path in files:
        name, is_package, package = info[path]
        unique = name
        n = 2
        while unique in modules:
            unique = f"{name}~{n}"
            n += 1
        name = unique
        relpath = path.relative_to(root).as_posix()
        try:
            with tokenize.open(str(path)) as handle:
                source = handle.read()
            tree = ast.parse(source, filename=relpath)
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError,
                LookupError, RecursionError) as exc:
            graph.errors.append({"path": relpath,
                                 "error": f"{type(exc).__name__}: {exc}",
                                 "line": getattr(exc, "lineno", 0) or 0})
            continue

        mod = ModuleInfo(
            name=name, relpath=relpath, is_package=is_package,
            package=package, tree=tree, lines=source.count("\n") + 1,
        )
        mod_symbol = Symbol(
            id=name, name=name.rsplit(".", 1)[-1], qualname=name, kind="module",
            module=name, path=relpath, line=1, end_line=mod.lines,
            doc=(ast.get_docstring(tree, clean=True) or "")[:600],
            signature=f"module {name}",
            is_test=is_test_path(relpath),
        )
        graph.add_symbol(mod_symbol)
        modules[name] = mod
        index_module(mod, graph)

    build_namespaces(graph, modules)
    Resolver(graph, modules).run()
    graph.modules = modules
    report = analyze(graph)
    report["dead_code"] = analyze_dead_code(
        graph, root, explicit_roots=roots or [], root_public=root_public,
        include_test_roots=include_test_roots,
        extra_decorators=framework_decorators or [])
    report["git"] = (analyze_git(root, graph, max_commits=git_commits)
                     if git else {"available": False, "reason": "disabled"})

    report["meta"] = {
        "tool": "progmap",
        "version": __version__,
        "root": str(root),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_ms": int((time.time() - started) * 1000),
        "errors": graph.errors,
    }
    report["modules"] = _module_dicts(graph)
    report["symbols"] = sorted((s.to_dict() for s in graph.symbols.values()),
                               key=lambda s: s["id"])
    report["edges"] = sorted((e.to_dict() for e in graph.edges.values()),
                             key=lambda e: (e["src"], e["dst"], e["kind"]))
    report["unresolved"] = graph.unresolved[:200]
    return graph, report


def _module_dicts(graph: Graph) -> list[dict]:
    grouped: dict[str, list[str]] = {}
    for sym in graph.symbols.values():
        grouped.setdefault(sym.module, []).append(sym.kind)
    out = []
    for mod in graph.modules.values():
        kinds = grouped.get(mod.name, [])
        out.append({
            "name": mod.name,
            "path": mod.relpath,
            "package": mod.package,
            "is_package": mod.is_package,
            "lines": mod.lines,
            "doc": (graph.symbols[mod.name].doc if mod.name in graph.symbols else ""),
            "symbols": len(kinds),
            "functions": sum(1 for k in kinds if k in ("function", "method", "nested")),
            "classes": sum(1 for k in kinds if k == "class"),
            "external": mod.external_imports,
            "stars": mod.stars,
        })
    return out
