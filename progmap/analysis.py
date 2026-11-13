"""Whole-project analysis: metrics, cycles, hubs."""

from __future__ import annotations

from collections import defaultdict

from .model import Graph

CALL_KINDS = {"calls", "instantiates"}


def analyze(graph: Graph) -> dict:
    _metrics(graph)
    call_cycles = _cycles(_call_adjacency(graph))
    module_cycles = _cycles(_module_adjacency(graph))
    return {
        "stats": _stats(graph),
        "hubs": _hubs(graph),
        "complex": _complex(graph),
        "cycles": {"functions": call_cycles, "modules": module_cycles},
        "module_edges": sorted(
            [e.to_dict() for e in graph.edges.values() if e.kind == "imports"],
            key=lambda e: (e["src"], e["dst"])),
    }


def _metrics(graph: Graph) -> None:
    for e in graph.edges.values():
        if e.kind in ("calls", "instantiates", "references"):
            src = graph.symbols.get(e.src)
            dst = graph.symbols.get(e.dst)
            if src:
                src.fan_out += 1
            if dst:
                dst.fan_in += 1


def _stats(graph: Graph) -> dict:
    kinds: dict[str, int] = defaultdict(int)
    for sym in graph.symbols.values():
        kinds[sym.kind] += 1
    edge_kinds: dict[str, int] = defaultdict(int)
    for edge in graph.edges.values():
        edge_kinds[edge.kind] += 1
    return {
        "files": len(graph.modules),
        "symbols": len(graph.symbols),
        "by_kind": dict(kinds),
        "edges": len(graph.edges),
        "by_edge": dict(edge_kinds),
        "loc": sum(m.lines for m in graph.modules.values()),
        "unresolved": len(graph.unresolved),
        "errors": len(graph.errors),
    }


def _hubs(graph: Graph, limit: int = 15) -> list[dict]:
    rows = [s for s in graph.symbols.values()
            if s.kind in ("function", "method")
            and not (s.name.startswith("__") and s.name.endswith("__"))]
    rows.sort(key=lambda s: (s.fan_in, s.fan_out), reverse=True)
    return [{"id": s.id, "label": s.qualname, "module": s.module,
             "fan_in": s.fan_in, "fan_out": s.fan_out} for s in rows[:limit]
            if s.fan_in > 0]


def _complex(graph: Graph, limit: int = 15) -> list[dict]:
    rows = [s for s in graph.symbols.values() if s.kind in ("function", "method")]
    rows.sort(key=lambda s: (s.complexity, s.loc), reverse=True)
    return [{"id": s.id, "label": s.qualname, "module": s.module,
             "complexity": s.complexity, "loc": s.loc} for s in rows[:limit]]


def _call_adjacency(graph: Graph) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges.values():
        if e.kind in CALL_KINDS:
            src = graph.symbols.get(e.src)
            dst = graph.symbols.get(e.dst)
            if src and dst and src.kind in ("function", "method", "nested") \
                    and dst.kind in ("function", "method", "nested"):
                adj[e.src].add(e.dst)
    return adj


def _module_adjacency(graph: Graph) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges.values():
        if (e.kind == "imports" and not e.type_only and not e.lazy
                and e.src in graph.modules and e.dst in graph.modules):
            adj[e.src].add(e.dst)
    return adj


def _cycles(adj: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components of size > 1 (iterative Kosaraju)."""
    nodes = set(adj) | {b for bs in adj.values() for b in bs}
    order: list[str] = []
    visited: set[str] = set()
    for start in sorted(nodes):
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[str, object]] = [(start, iter(sorted(adj.get(start, ()))))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:  # type: ignore[union-attr]
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, iter(sorted(adj.get(nxt, ())))))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()

    reverse: dict[str, list[str]] = defaultdict(list)
    for a, bs in adj.items():
        for b in bs:
            reverse[b].append(a)
    comps: list[list[str]] = []
    seen: set[str] = set()
    for node in reversed(order):
        if node in seen:
            continue
        seen.add(node)
        comp: list[str] = []
        stack = [node]
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for prev in reverse.get(cur, ()):
                if prev not in seen:
                    seen.add(prev)
                    stack.append(prev)
        if len(comp) > 1:
            comps.append(sorted(comp))
    comps.sort(key=len, reverse=True)
    return comps
