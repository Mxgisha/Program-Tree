"""Unused-code candidates: reachability from an explicit root set, with evidence.

The graph answers "what has no callers", but that is not the same as "unused".
This module builds a defensible root set (entry points, package exports, tests,
framework-decorated handlers, explicit user roots), marks everything reachable
through call/reference/inheritance edges, and reports only what is left — with
the reason and a confidence level, never a bare accusation.
"""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
from typing import Iterable

from .model import Edge, Graph, Symbol

LIVE_KINDS = {"calls", "instantiates", "references", "inherits"}
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}

# Decorators that usually mean "the framework will call this", not user code.
FRAMEWORK_DECORATORS = {
    "command", "group", "route", "get", "post", "put", "patch", "delete",
    "view", "endpoint", "api", "task", "job", "handler", "callback", "listener",
    "receiver", "fixture", "cron", "periodic", "subscribe", "webhook", "middleware",
    "rule", "register", "register_handler",
}


def analyze(graph: Graph, project_root: Path | None = None,
            explicit_roots: Iterable[str] = (), root_public: bool | None = None,
            include_test_roots: bool = True,
            extra_decorators: Iterable[str] = ()) -> dict:
    roots, test_roots, notes, fallback = _collect_roots(
        graph, project_root, explicit_roots, root_public, include_test_roots,
        set(extra_decorators))

    out_edges, in_edges, imports = _index_edges(graph)
    all_roots = set(roots) | set(test_roots)
    live = _reach(graph, all_roots, include_tests=True,
                  out_edges=out_edges, imports=imports)
    core_roots = set(roots)
    live_core = _reach(graph, core_roots, include_tests=False,
                       out_edges=out_edges, imports=imports)

    dead_modules = _dead_modules(graph, live)
    dead_module_names = {m["name"] for m in dead_modules}
    candidates = []
    test_only = []
    for sym in graph.symbols.values():
        if sym.kind == "module" or sym.is_test or sym.superseded:
            continue
        if sym.module in dead_module_names:
            continue
        if _is_dunder(sym.name):
            continue
        parent = graph.symbols.get(sym.parent) if sym.parent else None
        if parent is not None and parent.kind == "class" and parent.id not in live:
            continue  # implied by the dead class, reported once
        if sym.id in live:
            if sym.id not in live_core and sym.kind in ("function", "method", "nested", "class"):
                test_only.append(_test_only_entry(sym))
            continue
        candidates.append(_candidate(graph, sym, in_edges, live, dead_module_names))
    candidates.sort(key=lambda c: (CONFIDENCE_ORDER[c["confidence"]],
                                   c["kind"] != "class", c["module"], c["line"]))
    test_only.sort(key=lambda c: (c["module"], c["line"]))

    by_conf = defaultdict(int)
    for cand in candidates:
        by_conf[cand["confidence"]] += 1
    summary = {
        "roots": len(all_roots),
        "live": len(live),
        "candidates": len(candidates),
        "dead_modules": len(dead_modules),
        "test_only": len(test_only),
        "by_confidence": {k: by_conf.get(k, 0) for k in ("high", "medium", "low")},
        "fallback": fallback,
    }
    return {
        "summary": summary,
        "roots": [{"id": sid, "reason": reason}
                  for sid, reason in sorted(roots.items())][:200],
        "candidates": candidates,
        "dead_modules": dead_modules,
        "test_only": test_only[:50],
        "notes": notes,
    }


# ----------------------------------------------------------------------
# roots
# ----------------------------------------------------------------------
def _collect_roots(graph: Graph, project_root: Path | None,
                   explicit_roots: Iterable[str], root_public: bool | None,
                   include_test_roots: bool, extra_decorators: set[str]
                   ) -> tuple[dict[str, str], set[str], list[str], bool]:
    roots: dict[str, str] = {}
    test_roots: set[str] = set()
    notes: list[str] = []

    def add(sid: str | None, reason: str) -> None:
        if sid and sid in graph.symbols:
            roots.setdefault(sid, reason)

    scripts = _pyproject_scripts(project_root)
    is_library = not scripts and not any(
        m.has_main_guard or m.name.endswith("__main__")
        for m in graph.modules.values())
    public_roots = is_library if root_public is None else root_public
    module_calls: set[str] = set()
    imported: set[str] = set()
    for edge in graph.edges.values():
        if edge.kind == "calls" and edge.src in graph.modules:
            module_calls.add(edge.src)
        elif edge.kind == "imports":
            imported.add(edge.dst)

    for mod in graph.modules.values():
        parent_pkg = mod.name.rpartition(".")[0]
        tail = mod.name.rsplit(".", 1)[-1]
        module_symbol = graph.symbols.get(mod.name)
        is_test_module = bool(module_symbol and module_symbol.is_test)
        public_module = (not tail.startswith("_") and tail != "__main__"
                         and not is_test_module)
        if is_library and public_module:
            add(mod.name, "public module (library)")
        if is_library and mod.is_package and parent_pkg not in graph.modules:
            add(mod.name, "package entry point")
            for name, sid in mod.namespace.items():
                if not name.startswith("_"):
                    add(sid, f"package export ({name})")
        if mod.has_main_guard or mod.name.endswith("__main__"):
            add(mod.name, "__main__ entry point")
        for name in mod.all_names:
            add(mod.namespace.get(name), f"exported in __all__ ({name})")
        # A module with top-level calls that nothing imports is a script.
        if mod.name in module_calls and mod.name not in imported:
            add(mod.name, "script (top-level code, never imported)")

    for sym in graph.symbols.values():
        if sym.is_test:
            if include_test_roots and sym.kind in ("function", "method", "class"):
                test_roots.add(sym.id)
            continue
        deco = _framework_decorator(sym.decorators, extra_decorators)
        if deco:
            add(sym.id, f"framework decorator @{deco}")
        if public_roots and sym.parent == sym.module and sym.kind in ("function", "class") \
                and not sym.name.startswith("_"):
            add(sym.id, "public API (--root-public)")

    for spec in scripts:
        sid = resolve_root(graph, spec)
        if sid:
            add(sid, f"entry point ({spec})")
        else:
            notes.append(f"pyproject entry point not found: {spec}")

    for spec in explicit_roots:
        sid = resolve_root(graph, spec)
        if sid:
            add(sid, f"explicit root ({spec})")
        else:
            notes.append(f"root not found: {spec}")

    fallback = False
    if not roots:
        fallback = True
        for mod in graph.modules.values():
            roots[mod.name] = "fallback: no entry points detected"
    return roots, test_roots, notes, fallback


def resolve_root(graph: Graph, spec: str) -> str | None:
    spec = (spec or "").strip()
    if not spec:
        return None
    if spec in graph.symbols:
        return spec
    if spec in graph.modules:
        return spec
    if "::" in spec:
        return spec if spec in graph.symbols else None
    if ":" in spec:
        sid = spec.replace(":", "::", 1)
        return sid if sid in graph.symbols else None
    if "." in spec:
        mod, _, name = spec.rpartition(".")
        sid = f"{mod}::{name}"
        if sid in graph.symbols:
            return sid
    for sid, sym in graph.symbols.items():
        if sym.qualname == spec or sid.endswith("::" + spec) or sym.name == spec:
            return sid
    return None


def _framework_decorator(decorators: list[str],
                         extra: set[str] | None = None) -> str | None:
    for text in decorators:
        head = text.split("(")[0].strip()
        name = head.rsplit(".", 1)[-1]
        if name in FRAMEWORK_DECORATORS or name.startswith("on_") \
                or (extra and name in extra):
            return name
    return None


def _pyproject_scripts(root: Path | None) -> list[str]:
    if root is None:
        return []
    path = root / "pyproject.toml"
    if not path.exists():
        return []
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return []
    project = data.get("project", {}) or {}
    specs: list[str] = []
    for key in ("scripts", "gui-scripts"):
        specs.extend(v for v in (project.get(key, {}) or {}).values() if isinstance(v, str))
    for group in (project.get("entry-points", {}) or {}).values():
        if isinstance(group, dict):
            specs.extend(v for v in group.values() if isinstance(v, str))
    poetry = ((data.get("tool", {}) or {}).get("poetry", {}) or {}).get("scripts", {}) or {}
    specs.extend(v for v in poetry.values() if isinstance(v, str))
    cleaned = []
    for spec in specs:
        spec = spec.split()[0].split("[")[0]
        cleaned.append(spec)
    return cleaned


# ----------------------------------------------------------------------
# reachability
# ----------------------------------------------------------------------
def _index_edges(graph: Graph) -> tuple[dict[str, list[Edge]], dict[str, list[Edge]],
                                        dict[str, list[str]]]:
    out_edges: dict[str, list[Edge]] = defaultdict(list)
    in_edges: dict[str, list[Edge]] = defaultdict(list)
    imports: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges.values():
        if edge.kind in LIVE_KINDS:
            out_edges[edge.src].append(edge)
            in_edges[edge.dst].append(edge)
        elif edge.kind == "imports" and not edge.type_only:
            imports[edge.src].append(edge.dst)
    return out_edges, in_edges, imports


def _reach(graph: Graph, roots: set[str], include_tests: bool,
           out_edges: dict[str, list[Edge]],
           imports: dict[str, list[str]]) -> set[str]:
    live: set[str] = set()
    queue: deque[str] = deque()

    def allowed(sid: str) -> bool:
        sym = graph.symbols.get(sid)
        if sym is None:
            return False
        if not include_tests:
            mod = graph.symbols.get(sym.module)
            if mod is not None and mod.is_test:
                return False
        return True

    def mark(sid: str) -> None:
        if sid in live or not allowed(sid):
            return
        live.add(sid)
        queue.append(sid)

    for sid in roots:
        mark(sid)
    while queue:
        sid = queue.popleft()
        sym = graph.symbols[sid]
        if sym.kind == "module":
            parent = sym.id.rpartition(".")[0]
            if parent in graph.modules:
                mark(parent)  # importing a submodule executes its package
            mod = graph.modules.get(sid)
            if mod is not None:
                for child_id in mod.symbols:
                    child = graph.symbols.get(child_id)
                    if child is not None and child.parent == sid and _is_dunder(child.name):
                        mark(child_id)  # module __getattr__ / __dir__ hooks
            for dst in imports.get(sid, ()):  # importing executes the module
                mark(dst)
            for edge in out_edges.get(sid, ()):  # module-level code
                mark(edge.dst)
            continue
        mark(sym.module)  # a live symbol makes its module live
        if sym.kind == "class":
            for child in sym.attrs.values():
                mark(child)
            for base in sym.base_ids:
                mark(base)
        for edge in out_edges.get(sid, ()):
            mark(edge.dst)
        if sym.parent:
            parent = graph.symbols.get(sym.parent)
            if parent is not None and parent.kind == "class":
                mark(parent.id)  # a live method makes the class live
    return live


# ----------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------
def _dead_modules(graph: Graph, live: set[str]) -> list[dict]:
    out = []
    for mod in graph.modules.values():
        if mod.name in live:
            continue
        symbol = graph.symbols.get(mod.name)
        if symbol is not None and symbol.is_test:
            continue
        out.append({"name": mod.name, "path": mod.relpath, "lines": mod.lines,
                    "symbols": len(mod.symbols)})
    out.sort(key=lambda m: m["name"])
    return out


def _candidate(graph: Graph, sym: Symbol, in_edges, live: set[str],
               dead_modules: set[str]) -> dict:
    incoming = [e for e in in_edges.get(sym.id, ())
                if e.kind in ("calls", "instantiates", "references")
                and e.src != sym.id
                and _module_of(graph, e.src) not in dead_modules]
    callers = sorted({e.src for e in incoming})
    dead_callers = [c for c in callers if c not in live]
    module = graph.modules.get(sym.module)
    dynamic = bool(module and (module.dynamic or module.stars))
    private = sym.name.startswith("_")

    if not callers:
        reason = "no callers or references"
        if private and not dynamic:
            confidence = "high"
        elif dynamic:
            confidence = "low"
        else:
            confidence = "medium"
    elif dead_callers:
        labels = ", ".join(_short(c) for c in dead_callers[:3])
        reason = f"only used by dead code ({labels})"
        confidence = "medium" if private and not dynamic else "low"
    else:
        reason = "not reachable from any root"
        confidence = "low"

    category = "data" if sym.kind in ("variable", "attribute") else "code"
    if category == "code" and sym.parent == sym.module and not private:
        category = "public"
        if confidence == "high":
            confidence = "medium"
    if category == "data" and confidence == "high":
        confidence = "medium"  # module data is often used dynamically
    return {
        "id": sym.id, "label": sym.qualname, "name": sym.name, "kind": sym.kind,
        "module": sym.module, "path": sym.path, "line": sym.line,
        "reason": reason, "confidence": confidence, "category": category,
        "callers": callers[:5], "dynamic": dynamic,
    }


def _test_only_entry(sym: Symbol) -> dict:
    return {"id": sym.id, "label": sym.qualname, "kind": sym.kind,
            "module": sym.module, "path": sym.path, "line": sym.line,
            "reason": "only reached from tests"}


def _module_of(graph: Graph, sid: str) -> str:
    sym = graph.symbols.get(sid)
    return sym.module if sym is not None else ""


def _short(sid: str) -> str:
    return sid.rsplit("::", 1)[-1]


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")
