"""Self-contained HTML explorer: JSON is embedded, no server or CDN needed."""

from __future__ import annotations

import html
import json
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"
TEMPLATE = ASSETS / "explorer.html"


def render_html(report: dict, title: str) -> str:
    if not TEMPLATE.exists():
        return _fallback(report, title)
    template = TEMPLATE.read_text(encoding="utf-8")
    data = json.dumps(report, separators=(",", ":"), ensure_ascii=False)
    data = data.replace("</", "<\\/")
    return (template
            .replace("__PROGMAP_TITLE__", html.escape(title))
            .replace("__PROGMAP_DATA__", data))


def _fallback(report: dict, title: str) -> str:
    stats = report["stats"]
    return f"""<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title>
<h1>{html.escape(title)}</h1>
<p>{stats['symbols']} symbols, {stats['edges']} edges.
The explorer template is missing; see map.json and report.md.</p>"""
