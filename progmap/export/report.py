"""Markdown report: the parts of the map that read better as prose."""

from __future__ import annotations


def render_report(report: dict) -> str:
    meta = report["meta"]
    stats = report["stats"]
    lines = [
        f"# progmap: `{meta['root']}`",
        "",
        f"{stats['files']} files · {stats['loc']:,} lines · "
        f"{stats['symbols']:,} symbols · {stats['edges']:,} edges · "
        f"indexed in {meta['elapsed_ms']} ms",
        "",
    ]
    by_kind = stats["by_kind"]
    lines += [
        "## Shape",
        "",
        "| kind | count |",
        "| --- | ---: |",
    ]
    for kind in ("module", "class", "function", "method", "nested", "variable", "attribute"):
        if by_kind.get(kind):
            lines.append(f"| {kind} | {by_kind[kind]:,} |")
    lines += ["", "| edge | count |", "| --- | ---: |"]
    for kind, count in sorted(stats["by_edge"].items()):
        lines.append(f"| {kind} | {count:,} |")

    hubs = report.get("hubs") or []
    if hubs:
        lines += ["", "## Most-called functions", "",
                  "| function | module | fan-in | fan-out |",
                  "| --- | --- | ---: | ---: |"]
        for hub in hubs:
            lines.append(f"| `{hub['label']}` | `{hub['module']}` | "
                         f"{hub['fan_in']} | {hub['fan_out']} |")

    complex_ = report.get("complex") or []
    if complex_:
        lines += ["", "## Most complex functions", "",
                  "| function | module | complexity | lines |",
                  "| --- | --- | ---: | ---: |"]
        for row in complex_:
            lines.append(f"| `{row['label']}` | `{row['module']}` | "
                         f"{row['complexity']} | {row['loc']} |")

    module_cycles = report["cycles"]["modules"]
    if module_cycles:
        lines += ["", "## Import cycles", ""]
        for cycle in module_cycles[:20]:
            lines.append("- " + " → ".join(f"`{m}`" for m in cycle))
    else:
        lines += ["", "## Import cycles", "", "None — the module graph is a DAG."]

    call_cycles = report["cycles"]["functions"]
    if call_cycles:
        lines += ["", "## Recursive groups", ""]
        for cycle in call_cycles[:20]:
            names = " ↔ ".join(f"`{name.rsplit('::', 1)[-1]}`" for name in cycle[:6])
            more = "" if len(cycle) <= 6 else f" (+{len(cycle) - 6})"
            lines.append(f"- {names}{more}")

    dead = report.get("dead_code") or {}
    dsum = dead.get("summary") or {}
    if dsum:
        conf = dsum.get("by_confidence", {})
        lines += ["", "## Unused code", "",
                  f"{dsum.get('candidates', 0)} candidates "
                  f"({conf.get('high', 0)} high, {conf.get('medium', 0)} medium, "
                  f"{conf.get('low', 0)} low) from {dsum.get('roots', 0)} roots; "
                  f"{dsum.get('dead_modules', 0)} dead modules."]
        if dsum.get("fallback"):
            lines += ["", "_No entry points were detected, so every module was treated as a root._"]
        cands = dead.get("candidates") or []
        if cands:
            lines += ["", "| symbol | kind | module | reason | confidence |",
                      "| --- | --- | --- | --- | --- |"]
            for cand in cands[:40]:
                lines.append(f"| `{cand['label']}` | {cand['kind']} | "
                             f"`{cand['module']}` | {cand['reason']} | "
                             f"{cand['confidence']} |")
            if len(cands) > 40:
                lines.append(f"| _… {len(cands) - 40} more_ | | | | |")
        else:
            lines += ["", "No unused-code candidates found."]
        dead_modules = dead.get("dead_modules") or []
        if dead_modules:
            lines += ["", "### Dead modules", ""]
            for mod in dead_modules[:20]:
                lines.append(f"- `{mod['name']}` ({mod['lines']:,} lines, "
                             f"{mod['symbols']} symbols)")
        test_only = dead.get("test_only") or []
        if test_only:
            lines += ["", "### Only reached from tests", ""]
            for row in test_only[:20]:
                lines.append(f"- `{row['label']}` (`{row['module']}`)")

    git = report.get("git") or {}
    if git.get("available"):
        lines += ["", "## Change coupling", "",
                  f"{git.get('commits', 0):,} commits · "
                  f"{git.get('modules', 0)} modules with history"]
        cochange = git.get("cochange") or []
        if cochange:
            lines += ["", "| module A | module B | together | confidence | lift |",
                      "| --- | --- | ---: | ---: | ---: |"]
            for row in cochange[:20]:
                lines.append(f"| `{row['a']}` | `{row['b']}` | {row['count']} | "
                             f"{row['confidence']:.2f} | {row['lift']:.2f} |")
        hotspots = git.get("hotspots") or []
        if hotspots:
            lines += ["", "### Hotspots (churn × complexity)", "",
                      "| function | module | churn | complexity | score |",
                      "| --- | --- | ---: | ---: | ---: |"]
            for row in hotspots[:10]:
                lines.append(f"| `{row['label']}` | `{row['module']}` | "
                             f"{row['churn']} | {row['complexity']} | {row['score']} |")

    lines += ["", "## Largest modules", "",
              "| module | lines | functions | classes |",
              "| --- | ---: | ---: | ---: |"]
    for mod in sorted(report["modules"], key=lambda m: m["lines"], reverse=True)[:15]:
        lines.append(f"| `{mod['name']}` | {mod['lines']:,} | "
                     f"{mod['functions']} | {mod['classes']} |")

    external = sorted({e for m in report["modules"] for e in m["external"]})
    if external:
        lines += ["", "## External imports", "",
                  ", ".join(f"`{e}`" for e in external[:80])]

    if report["unresolved"]:
        lines += ["", f"## Unresolved names ({stats['unresolved']})", ""]
        seen = set()
        for row in report["unresolved"][:40]:
            key = (row["name"], row["module"])
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- `{row['name']}` in `{row['module']}`:{row['line']}")
        lines += ["", "_These may be dynamic imports, `exec`, globals, or C extensions._"]

    if meta.get("errors"):
        lines += ["", "## Parse errors", ""]
        for err in meta["errors"][:30]:
            lines.append(f"- `{err['path']}`: {err['error']}")
    lines.append("")
    return "\n".join(lines)
