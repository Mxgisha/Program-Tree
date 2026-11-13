"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .export.dot import render_dot
from .export.html import render_html
from .export.report import render_report
from .pipeline import build_graph


def _load_config(root: Path) -> dict:
    path = root / ".progmap.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    config = data if isinstance(data, dict) else {}
    for key in ("roots", "exclude", "framework_decorators"):
        if not isinstance(config.get(key, []), list):
            config[key] = []
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="progmap",
        description="Map a Python codebase: functions, classes, calls, imports, cycles.",
    )
    parser.add_argument("--version", action="version", version=f"progmap {__version__}")
    parser.add_argument("path", nargs="?", default=".", help="directory to analyze")
    parser.add_argument("-o", "--out", default="progmap-out", help="output directory")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="skip paths matching GLOB (repeatable)")
    parser.add_argument("--dot-limit", type=int, default=400,
                        help="max nodes in graph.dot (default 400)")
    parser.add_argument("--no-dot", action="store_true", help="skip graph.dot")
    parser.add_argument("--no-report", action="store_true", help="skip report.md")
    parser.add_argument("--root", action="append", default=[], metavar="SYMBOL",
                        help="treat SYMBOL/module as an entry point (repeatable)")
    parser.add_argument("--root-public", dest="root_public", action="store_true",
                        default=None, help="treat public top-level symbols as roots")
    parser.add_argument("--no-root-public", dest="root_public", action="store_false",
                        help="audit public top-level symbols too")
    parser.add_argument("--no-test-roots", dest="test_roots", action="store_false",
                        default=True, help="do not treat tests as entry points")
    parser.add_argument("--git", dest="git", action="store_true", default=None,
                        help="use git history signals (default: on when available)")
    parser.add_argument("--no-git", dest="git", action="store_false")
    parser.add_argument("--framework-decorator", action="append", default=[],
                        metavar="NAME", help="extra decorator that marks a root (repeatable)")
    parser.add_argument("--git-commits", type=int, default=None,
                        help="max commits to scan (default 1500)")
    args = parser.parse_args(argv)

    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"progmap: not a directory: {root}", file=sys.stderr)
        return 2
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    config = _load_config(root)
    excludes = list(config.get("exclude", [])) + args.exclude
    roots = list(config.get("roots", [])) + args.root
    root_public = args.root_public
    if root_public is None:
        root_public = config.get("root_public")
    framework_decorators = list(config.get("framework_decorators", [])) \
        + args.framework_decorator
    git = args.git if args.git is not None else bool(config.get("git", True))
    git_commits = args.git_commits or int(config.get("git_commits", 1500))
    _graph, report = build_graph(
        root, excludes=excludes, roots=roots, root_public=root_public,
        include_test_roots=args.test_roots, git=git, git_commits=git_commits,
        framework_decorators=framework_decorators)
    if not report["symbols"]:
        print("progmap: no Python files found", file=sys.stderr)
        return 1

    (out / "map.json").write_text(
        json.dumps(report, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    title = f"progmap · {root.name}"
    (out / "explorer.html").write_text(render_html(report, title), encoding="utf-8")
    if not args.no_report:
        (out / "report.md").write_text(render_report(report), encoding="utf-8")
    if not args.no_dot:
        (out / "graph.dot").write_text(
            render_dot(report, args.dot_limit), encoding="utf-8")

    stats = report["stats"]
    print(f"{stats['files']} files · {stats['loc']:,} lines · "
          f"{stats['symbols']:,} symbols · {stats['edges']:,} edges · "
          f"{report['meta']['elapsed_ms']} ms")
    if stats["unresolved"]:
        print(f"note: {stats['unresolved']} names unresolved "
              f"(dynamic code, C extensions, or globals)")
    if report["meta"]["errors"]:
        print(f"note: {stats['errors']} files could not be parsed")
    dead = report.get("dead_code", {})
    dsum = dead.get("summary", {})
    if dsum:
        conf = dsum.get("by_confidence", {})
        dead_mods = dsum.get("dead_modules", 0)
        print(f"unused: {dsum.get('candidates', 0)} candidates "
              f"({conf.get('high', 0)} high, {conf.get('medium', 0)} medium, "
              f"{conf.get('low', 0)} low) · {dead_mods} dead "
              f"{'module' if dead_mods == 1 else 'modules'}")
    gsum = report.get("git", {})
    if gsum.get("available"):
        print(f"git: {gsum['commits']} commits · {len(gsum.get('cochange', []))} coupled pairs")
    print(f"\n  {out / 'explorer.html'}\n  {out / 'map.json'}")
    return 0
