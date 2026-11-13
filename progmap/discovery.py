"""Find Python files and give each one a stable dotted module name."""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", "virtualenv",
    "node_modules", "build", "dist", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".eggs", "site-packages", ".direnv", ".idea", ".vscode",
}
# Directories that are commonly source roots rather than packages.
SRC_ROOTS = {"src", "lib", "python"}


def discover_files(root: Path, excludes: list[str] | None = None) -> list[Path]:
    """Return Python files under *root*, newest-stable order, honoring excludes.

    If *root* is inside a git work tree, ``git ls-files`` is used so that
    .gitignore is respected for free.
    """
    excludes = excludes or []
    files = _git_files(root)
    if files is None:
        files = _walk_files(root)
    out = []
    for p in files:
        rel = p.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(rel, pat) for pat in excludes):
            continue
        out.append(p)
    return sorted(out)


def _git_files(root: Path) -> list[Path] | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard", "--", "*.py"],
            capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    names = [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]
    if not names:
        return None
    paths = []
    for n in names:
        p = (root / n).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            continue
        if p.is_file():
            paths.append(p)
    return paths


def _walk_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name.endswith(".py"):
                paths.append(Path(dirpath) / name)
    return paths


def _source_roots(root: Path) -> set[str]:
    """Top-level dirs like ``src/`` that should not appear in module names."""
    out = set()
    try:
        for entry in root.iterdir():
            if (entry.is_dir() and entry.name in SRC_ROOTS
                    and not (entry / "__init__.py").exists()):
                if any(entry.rglob("*.py")):
                    out.add(entry.name)
    except OSError:
        pass
    return out


def module_name_for(path: Path, root: Path, src_roots: set[str],
                    prefix: str = "") -> tuple[str, bool, str]:
    """Return (dotted module name, is_package, package).

    ``src/pkg/mod.py`` -> ``pkg.mod``; ``pkg/__init__.py`` -> ``pkg``.
    """
    rel = path.relative_to(root)
    parts = list(rel.with_suffix("").parts)
    is_package = parts and parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    if parts and parts[0] in src_roots:
        parts = parts[1:]
    # A directory without __init__.py is still a namespace package in py3;
    # keeping its name makes imports line up far more often than dropping it.
    local = ".".join(parts)
    if prefix:
        local = f"{prefix}.{local}" if local else prefix
    name = local or path.stem
    package = name if is_package else (name.rpartition(".")[0] or "")
    return name, is_package, package


def prepare(root: Path, excludes: list[str] | None = None
            ) -> tuple[list[Path], dict[Path, tuple[str, bool, str]]]:
    files = discover_files(root, excludes)
    roots = _source_roots(root)
    # Analyzing a package directory directly: qualify modules with its name so
    # they cannot collide with stdlib modules (e.g. click/types.py vs types).
    prefix = root.name if (root / "__init__.py").exists() else ""
    info = {}
    for p in files:
        info[p] = module_name_for(p, root, roots, prefix)
    return files, info
