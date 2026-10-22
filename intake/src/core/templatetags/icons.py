"""
`{% icon "name" %}` — inline a Tabler SVG from `ui/static/icons/<name>.svg`.

The SVG file is loaded once per process and cached. If it's missing we render
a tiny placeholder square instead of crashing the page — makes scaffolding the
UI before we've copied every icon less painful.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from django import template
from django.conf import settings
from django.utils.safestring import mark_safe

register = template.Library()

_ICONS_DIR = Path(settings.BASE_DIR) / "ui" / "static" / "icons"

_PLACEHOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" '
    'width="{size}" height="{size}" class="{cls}">'
    '<rect x="4" y="4" width="16" height="16" rx="3"/>'
    "</svg>"
)


@lru_cache(maxsize=256)
def _read_icon(name: str) -> str | None:
    path = _ICONS_DIR / f"{name}.svg"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


@register.simple_tag
def icon(name: str, size: int = 20, cls: str = "") -> str:
    raw = _read_icon(name)
    if raw is None:
        return mark_safe(_PLACEHOLDER.format(size=size, cls=cls))  # noqa: S308
    # Inject width/height/class onto the root <svg>. Tabler SVGs already have
    # stroke="currentColor", so they inherit color automatically.
    svg = raw.replace(
        "<svg",
        f'<svg width="{size}" height="{size}" class="{cls}"',
        1,
    )
    return mark_safe(svg)  # noqa: S308
