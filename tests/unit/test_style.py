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


STICKY = re.compile(r'\[data-testid="stLayoutWrapper"\]:has\(> \.st-key-masthead\)\s*\{([^}]*)\}')


def test_only_the_banner_is_positioned_and_it_sticks_to_the_top() -> None:
    blocks = STICKY.findall(_rules())
    assert len(blocks) == 1
    assert re.search(r"position:\s*sticky;", blocks[0]) and re.search(r"\btop:", blocks[0])


def test_css_never_touches_layout_or_forces_rules() -> None:
    rules = STICKY.sub("", _rules())  # the sticky banner is the one allowed exception
    properties = set(re.findall(r"^\s*([a-z-]+)\s*:", rules, flags=re.MULTILINE))
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
    for name in (
        "style.MASTHEAD",
        "style.WELCOME",
        "style.SUGGESTIONS",
        "style.user_key",
        "style.bot_key",
        "style.result_key",
    ):
        assert name in APP


def test_banner_ships_with_the_app_and_has_alt_text() -> None:
    assert style.BANNER_PATH.read_bytes().startswith(b"\x89PNG")
    html = style.banner_html()
    assert html.startswith('<img src="data:image/png;base64,') and f'alt="{style.BANNER_ALT}"' in html
    assert "st.html(style.banner_html())" in APP


def test_scroll_script_targets_the_keyed_container_under_the_banner() -> None:
    script = style.scroll_below_banner_js(style.user_key("pending"))
    assert script.startswith("<script>") and script.endswith("</script>")
    assert '".st-key-user-pending"' in script and '".st-key-masthead"' in script
    assert "scrollTo" in script and "tries < 40" in script  # bounded wait for the element to mount


def test_app_scrolls_to_the_pending_question_only() -> None:
    assert APP.count("scroll_here=True") == 1 and 'user_bubble(question, "pending", scroll_here=True)' in APP
