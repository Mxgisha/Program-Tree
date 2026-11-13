"""Git history signals: churn, change coupling, hotspots.

Static structure shows what *could* call what. History shows what actually
changes together — a relationship no parser can see. This is intentionally
cheap: one `git log --name-only` pass, no blame, no full diffs.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

from .model import Graph

MAX_COMMIT_FILES = 40  # skip mass renames / vendored drops
COCHANGE_LIMIT = 40


def analyze(root: Path, graph: Graph, max_commits: int = 1500,
            min_cochanges: int = 2) -> dict:
    if not _git_ok(root):
        return {"available": False, "reason": "not a git repository"}
    commits = _read_commits(root, max_commits)
    if commits is None:
        return {"available": False, "reason": "git log failed"}

    path_to_module = {mod.relpath: mod.name for mod in graph.modules.values()}
    support: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    kept = 0
    for changed in commits:
        modules = sorted({path_to_module[p] for p in changed if p in path_to_module})
        if not modules or len(modules) > MAX_COMMIT_FILES:
            continue
        kept += 1
        for module in modules:
            support[module] += 1
        for i, a in enumerate(modules):
            for b in modules[i + 1:]:
                pairs[(a, b)] += 1

    total = sum(support.values()) or 1
    cochange = []
    for (a, b), count in pairs.items():
        if count < min_cochanges:
            continue
        base = min(support[a], support[b])
        confidence = count / base if base else 0.0
        if confidence < 0.3:
            continue
        lift = (count * (kept or 1)) / max(1, support[a] * support[b])
        cochange.append({"a": a, "b": b, "count": count,
                         "confidence": round(confidence, 2),
                         "lift": round(lift, 2)})
    cochange.sort(key=lambda x: (-x["confidence"], -x["count"]))
    cochange = cochange[:COCHANGE_LIMIT]

    hotspots = []
    for sym in graph.symbols.values():
        if sym.kind not in ("function", "method"):
            continue
        churn = support.get(sym.module, 0)
        if not churn:
            continue
        score = churn * sym.complexity
        hotspots.append({"id": sym.id, "label": sym.qualname, "module": sym.module,
                         "churn": churn, "complexity": sym.complexity,
                         "loc": sym.loc, "score": score})
    hotspots.sort(key=lambda row: row["score"], reverse=True)

    return {
        "available": True,
        "commits": kept,
        "requested": max_commits,
        "modules": len(support),
        "cochange": cochange,
        "churn": dict(sorted(support.items())),
        "hotspots": hotspots[:15],
    }


def _git_ok(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == b"true"


def _read_commits(root: Path, max_commits: int) -> list[list[str]] | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "log", "--no-merges", "--name-only",
             "--relative", "--pretty=format:@@%H", f"-n{max_commits}"],
            capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    commits: list[list[str]] = []
    current: list[str] = []
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if line.startswith("@@"):
            if current:
                commits.append(current)
            current = []
        elif line:
            current.append(line)
    if current:
        commits.append(current)
    return commits
