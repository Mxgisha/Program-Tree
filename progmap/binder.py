"""Bind module-level names to definitions: imports, aliases, re-exports."""

from __future__ import annotations

import ast
from typing import Iterator

from .model import Graph, ModuleInfo

EXT = "@ext:"


def is_external(value: str | None) -> bool:
    return bool(value) and value.startswith(EXT)


def absolute_from_relative(mod: ModuleInfo, level: int, target: str | None) -> str:
    if level == 0:
        return target or ""
    package = mod.name if mod.is_package else mod.name.rpartition(".")[0]
    parts = package.split(".") if package else []
    up = level - 1
    if up:
        parts = parts[:-up] if up <= len(parts) else []
    base = ".".join(parts)
    if target:
        base = f"{base}.{target}" if base else target
    return base


def build_namespaces(graph: Graph, modules: dict[str, ModuleInfo]) -> None:
    # Every module starts with its own top-level definitions.
    for mod in modules.values():
        for sid in mod.symbols:
            sym = graph.symbols[sid]
            if sym.parent == mod.name:
                mod.namespace[sym.name] = sid  # last definition wins

    # Imports can re-export names, so iterate until nothing new appears.
    for _ in range(10):
        changed = False
        for mod in modules.values():
            if _bind_module_imports(mod, graph, modules):
                changed = True
        if not changed:
            break


def _bind_module_imports(mod: ModuleInfo, graph: Graph,
                         modules: dict[str, ModuleInfo]) -> bool:
    changed = False
    for node, type_only in _iter_imports(mod.tree.body if mod.tree else []):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                bound = alias.asname or name.split(".")[0]
                target = name if alias.asname else name.split(".")[0]
                value = target if target in modules else EXT + target
                if target in modules:
                    graph.add_edge(mod.name, target, "imports", node.lineno,
                                   type_only=type_only)
                else:
                    _note_external(mod, target)
                changed |= _bind(mod, bound, value)
            continue

        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            base = absolute_from_relative(mod, node.level, node.module)
            internal = base in modules
            if not internal:
                _note_external(mod, base or "?")
            base_edge_needed = True
            for alias in node.names:
                if alias.name == "*":
                    if internal and base not in mod.stars:
                        mod.stars.append(base)
                        changed = True
                    elif not internal:
                        _note_external(mod, base)
                    continue
                value = _resolve_imported_name(base, alias.name, modules, internal)
                if value is None:
                    continue
                if value in modules:
                    # ``from pkg import submodule`` depends on the submodule.
                    graph.add_edge(mod.name, value, "imports", node.lineno,
                                   type_only=type_only)
                    base_edge_needed = False
                changed |= _bind(mod, alias.asname or alias.name, value)
            if internal and base_edge_needed:
                graph.add_edge(mod.name, base, "imports", node.lineno,
                               type_only=type_only)
    return changed


def _resolve_imported_name(base: str, name: str, modules: dict[str, ModuleInfo],
                           internal: bool) -> str | None:
    candidate = f"{base}.{name}" if base else name
    if candidate in modules:  # ``from pkg import submodule``
        return candidate
    if internal:
        target = modules[base].namespace.get(name)
        if target is not None:
            return target
        return None  # maybe a later pass / star import; retry
    return EXT + (f"{base}.{name}" if base else name)


def _bind(mod: ModuleInfo, name: str, value: str) -> bool:
    if mod.namespace.get(name) == value:
        return False
    if name in mod.namespace and value.startswith(EXT):
        return False  # never downgrade a resolved name to "external"
    mod.namespace[name] = value
    return True


def _note_external(mod: ModuleInfo, name: str) -> None:
    if name and name not in mod.external_imports:
        mod.external_imports.append(name)


def _iter_imports(stmts: list[ast.stmt], type_only: bool = False
                  ) -> Iterator[tuple[ast.stmt, bool]]:
    """Yield (import statement, type_only) at module scope.

    Imports guarded by ``if TYPE_CHECKING:`` are still real type dependencies but
    must not be counted as runtime dependencies (they cannot create a runtime
    import cycle).
    """
    for stmt in stmts:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            yield stmt, type_only
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        elif isinstance(stmt, ast.If):
            guarded = type_only or _is_type_checking(stmt.test)
            for inner in stmt.body:
                yield from _iter_imports([inner], guarded)
            for inner in stmt.orelse:
                yield from _iter_imports([inner], type_only)
        else:
            for block in _blocks(stmt):
                yield from _iter_imports(block, type_only)


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _blocks(stmt: ast.stmt) -> list[list[ast.stmt]]:
    out = []
    for attr in ("body", "orelse", "finalbody"):
        block = getattr(stmt, attr, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            out.append(block)
    for handler in getattr(stmt, "handlers", []) or []:
        out.append(handler.body)
    for case in getattr(stmt, "cases", []) or []:
        out.append(case.body)
    return out
