"""Graphviz DOT export (static, for quick sharing or printing)."""

from __future__ import annotations

import re

PALETTE = {
    "function": ("#4f8cff", "#eaf2ff"),
    "method": ("#38b2ac", "#e6fbf7"),
    "nested": ("#8b7cf6", "#f0edff"),
    "class": ("#f6ad55", "#fff4e5"),
}


def render_dot(report: dict, limit: int = 400) -> str:
    symbols = {s["id"]: s for s in report["symbols"]}
    edges = [e for e in report["edges"] if e["kind"] in ("calls", "instantiates")]

    degree: dict[str, int] = {}
    for edge in edges:
        degree[edge["src"]] = degree.get(edge["src"], 0) + 1
        degree[edge["dst"]] = degree.get(edge["dst"], 0) + 1
    ranked = sorted(degree, key=degree.get, reverse=True)[:limit]
    keep = set(ranked)
    if not keep:
        return "digraph progmap {}\n"

    by_module: dict[str, list[str]] = {}
    for sid in ranked:
        by_module.setdefault(symbols[sid]["module"], []).append(sid)

    out = ["digraph progmap {", '  graph [rankdir=LR, fontname="Helvetica",'
           ' bgcolor="white", pad=0.3, nodesep=0.25, ranksep=0.8];',
           '  node [shape=box, style="rounded,filled", fontname="Helvetica",'
           ' fontsize=9, color="#334155", fontcolor="#0f172a"];',
           '  edge [color="#94a3b8", arrowsize=0.6];']
    for module, ids in sorted(by_module.items()):
        out.append(f'  subgraph "cluster_{_eid(module)}" {{')
        out.append(f'    label="{_esc(module)}"; fontsize=10; color="#e2e8f0";'
                   ' style="rounded";')
        for sid in ids:
            sym = symbols[sid]
            label = sym["qualname"].rsplit(".", 1)[-1]
            stroke, fill = PALETTE.get(sym["kind"], ("#a0aec0", "#f4f6f8"))
            out.append(f'    "{_eid(sid)}" [label="{_esc(label)}", '
                       f'fillcolor="{fill}", color="{stroke}"];')
        out.append("  }")
    for edge in edges:
        if edge["src"] not in keep or edge["dst"] not in keep:
            continue
        style = "dashed" if edge["kind"] == "instantiates" else "solid"
        out.append(f'  "{_eid(edge["src"])}" -> "{_eid(edge["dst"])}" '
                   f'[style={style}];')
    out.append("}")
    return "\n".join(out) + "\n"


def _eid(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
