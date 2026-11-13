"""Depth layer (BRAND.md, "Profundidade"): container keys that ``style.css`` paints.

Rule: layout comes from the Streamlit API (``st.container`` width/alignment, columns); the CSS
only paints surfaces (background, border, radius, shadow, text color). It targets the
``st-key-<key>`` classes Streamlit derives from our own keys, plus a few ``data-testid``
attributes, never generated class names. ``tests/unit/test_style.py`` enforces both rules.
"""

from __future__ import annotations

import base64
from functools import cache
from pathlib import Path
from typing import Final

CSS_PATH: Final = Path(__file__).with_name("style.css")
BANNER_PATH: Final = Path(__file__).with_name("banner.png")  # banner provided with the challenge (BRAND.md v1.1)
BANNER_ALT: Final = "Franq · Finance is Personal · #EuFaçoParte"

MASTHEAD: Final = "masthead"  # banner, fixed at the top while the page scrolls
HEADLINE: Final = "headline"
WELCOME: Final = "welcome"
SUGGESTIONS: Final = "suggestions"
KEY_PREFIXES: Final = (MASTHEAD, HEADLINE, WELCOME, SUGGESTIONS, "user-", "bot-", "result-")


def user_key(turn: int | str) -> str:
    return f"user-{turn}"


def bot_key(turn: int | str) -> str:
    return f"bot-{turn}"


def result_key(response_id: str) -> str:
    return f"result-{response_id}"


@cache
def banner_html() -> str:
    """Inline <img> with alt text (``st.image`` has none and adds a fullscreen button)."""
    data = base64.b64encode(BANNER_PATH.read_bytes()).decode("ascii")
    return f'<img src="data:image/png;base64,{data}" alt="{BANNER_ALT}" style="width:100%;display:block">'
