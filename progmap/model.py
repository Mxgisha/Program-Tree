"""Core data model: symbols, edges, modules, and the graph container."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Symbol:
    id: str
    name: str
    qualname: str
    kind: str  # module | class | function | method | nested | variable | attribute
    module: str
    path: str
    line: int
    end_line: int
    signature: str = ""
    doc: str = ""
    decorators: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    base_ids: list[str] = field(default_factory=list)
    is_async: bool = False
    complexity: int = 1
    loc: int = 1
    parent: str | None = None
    returns: str = ""
    return_id: str = ""
    annotation: str = ""
    type_id: str = ""
    param_types: dict[str, str] = field(default_factory=dict)
    param_ids: dict[str, str] = field(default_factory=dict)
    attrs: dict[str, str] = field(default_factory=dict)
    instance_attrs: dict[str, str] = field(default_factory=dict)
    is_test: bool = False
    fan_in: int = 0
    fan_out: int = 0
    superseded: bool = False

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "name": self.name,
            "qualname": self.qualname,
            "kind": self.kind,
            "module": self.module,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "signature": self.signature,
            "doc": self.doc,
            "decorators": self.decorators,
            "bases": self.bases,
            "base_ids": self.base_ids,
            "is_async": self.is_async,
            "complexity": self.complexity,
            "loc": self.loc,
            "parent": self.parent,
            "returns": self.returns,
            "return_id": self.return_id,
            "annotation": self.annotation,
            "type_id": self.type_id,
            "param_types": self.param_types,
            "param_ids": self.param_ids,
            "attrs": self.attrs,
            "instance_attrs": self.instance_attrs,
            "is_test": self.is_test,
            "fan_in": self.fan_in,
            "fan_out": self.fan_out,
        }
        # Compact payload: drop empty values, but keep identity fields.
        keep_empty = {"id", "name", "kind", "line"}
        return {k: v for k, v in out.items()
                if k in keep_empty or not (v is None or v is False or v == ""
                                           or v == [] or v == {})}


@dataclass
class Edge:
    src: str
    dst: str
    kind: str  # calls | instantiates | references | inherits | decorates | imports
    line: int = 0
    count: int = 1
    type_only: bool = False
    lazy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"src": self.src, "dst": self.dst, "kind": self.kind,
                "line": self.line, "count": self.count,
                "type_only": self.type_only, "lazy": self.lazy}


@dataclass
class ModuleInfo:
    name: str
    relpath: str
    is_package: bool
    package: str
    tree: ast.Module | None = None
    symbols: list[str] = field(default_factory=list)
    nodes: dict[str, ast.AST] = field(default_factory=dict)
    node_map: dict[int, str] = field(default_factory=dict)
    namespace: dict[str, str] = field(default_factory=dict)
    stars: list[str] = field(default_factory=list)
    external_imports: list[str] = field(default_factory=list)
    all_names: list[str] = field(default_factory=list)
    has_main_guard: bool = False
    dynamic: bool = False
    lines: int = 0


class Graph:
    def __init__(self) -> None:
        self.symbols: dict[str, Symbol] = {}
        self.edges: dict[tuple[str, str, str], Edge] = {}
        self.modules: dict[str, ModuleInfo] = {}
        self.errors: list[dict[str, Any]] = []
        self.unresolved: list[dict[str, Any]] = []

    # -- construction -------------------------------------------------
    def add_symbol(self, sym: Symbol) -> Symbol:
        if sym.id in self.symbols:
            n = 2
            while f"{sym.id}~{n}" in self.symbols:
                n += 1
            sym.id = f"{sym.id}~{n}"
        self.symbols[sym.id] = sym
        return sym

    def add_edge(self, src: str, dst: str, kind: str, line: int = 0,
                 type_only: bool = False, lazy: bool = False) -> None:
        if not src or not dst:
            return
        if src == dst and kind != "calls":
            return
        key = (src, dst, kind)
        edge = self.edges.get(key)
        if edge is None:
            self.edges[key] = Edge(src, dst, kind, line,
                                   type_only=type_only, lazy=lazy)
        else:
            edge.count += 1
            if not type_only:
                edge.type_only = False
            if not lazy:
                edge.lazy = False
            if line and (edge.line == 0 or line < edge.line):
                edge.line = line
