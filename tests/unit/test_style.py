"""The depth layer only paints, and only targets keys the app declares."""

from __future__ import annotations

import re
from pathlib import Path

from src.ui import style

CSS = style.CSS_PATH.read_text(encoding="utf-8")
APP = (Path(__file__).parents[2] / "app.py").read_text(encoding="utf-8")


def _rules() -> str:
    return re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL)


def test_css_targets_only_declared_keys() -> None:
    used = set(re.findall(r"st-key-([a-z]+-?)", _rules()))
    assert used and used <= set(style.KEY_PREFIXES)


def test_css_never_touches_layout_or_forces_rules() -> None:
    properties = set(re.findall(r"^\s*([a-z-]+)\s*:", _rules(), flags=re.MULTILINE))
    forbidden = {"display", "position", "float", "top", "left", "right", "bottom", "width", "height", "inset"}
    forbidden_prefixes = ("margin", "flex", "grid", "max-", "min-", "align-", "justify-")
    assert not properties & forbidden
    assert not [p for p in properties if p.startswith(forbidden_prefixes)]
    assert "!important" not in CSS


def test_css_survives_the_html_sanitizer() -> None:
    # DOMPurify (used by st.html) drops a <style> whose text has "<" followed by a letter.
    assert "<" not in CSS


def test_css_has_no_generated_class_names() -> None:
    assert not re.search(r"\.(css|st-emotion-cache)-[a-z0-9]+", CSS)


def test_app_loads_the_css_and_uses_every_key() -> None:
    assert "st.html(style.CSS_PATH)" in APP
    for name in ("style.WELCOME", "style.SUGGESTIONS", "style.user_key", "style.bot_key", "style.result_key"):
        assert name in APP
