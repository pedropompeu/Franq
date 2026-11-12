"""Depth layer (BRAND.md, "Profundidade"): container keys that ``style.css`` paints.

Rule: layout comes from the Streamlit API (``st.container`` width/alignment, columns); the CSS
only paints surfaces (background, border, radius, shadow, text color). It targets the
``st-key-<key>`` classes Streamlit derives from our own keys, plus a few ``data-testid``
attributes, never generated class names. ``tests/unit/test_style.py`` enforces both rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

CSS_PATH: Final = Path(__file__).with_name("style.css")

WELCOME: Final = "welcome"
SUGGESTIONS: Final = "suggestions"
KEY_PREFIXES: Final = (WELCOME, SUGGESTIONS, "user-", "bot-", "result-")


def user_key(turn: int | str) -> str:
    return f"user-{turn}"


def bot_key(turn: int | str) -> str:
    return f"bot-{turn}"


def result_key(response_id: str) -> str:
    return f"result-{response_id}"
