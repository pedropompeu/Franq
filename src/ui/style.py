"""Depth layer (BRAND.md, "Profundidade"): container keys that ``style.css`` paints.

Rule: layout comes from the Streamlit API (``st.container`` width/alignment, columns); the CSS
only paints surfaces (background, border, radius, shadow, text color). It targets the
``st-key-<key>`` classes Streamlit derives from our own keys, plus a few ``data-testid``
attributes, never generated class names. ``tests/unit/test_style.py`` enforces both rules.
"""

from __future__ import annotations

import base64
import json
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


def scroll_below_banner_js(key: str) -> str:
    """Script that scrolls the page until the keyed container sits right under the sticky banner.

    Streamlit has no scroll API. Used when a question is sent: after a suggestion click the welcome
    fills the screen and the live progress would sit below the fold. Waits up to 2 s for the
    element to mount. ``key`` comes from this module, never from user input.
    """
    selector = json.dumps(f".st-key-{key}")
    banner = json.dumps(f".st-key-{MASTHEAD}")
    return (
        "<script>(() => { let tries = 0; const go = () => {"
        f" const el = document.querySelector({selector});"
        " const main = document.querySelector('[data-testid=\"stMain\"]');"
        " if (!el || !main) { if (++tries < 40) setTimeout(go, 50); return; }"
        f" const top = document.querySelector({banner})?.getBoundingClientRect().bottom ?? 0;"
        " main.scrollTo({top: main.scrollTop + el.getBoundingClientRect().top - top - 16, behavior: 'smooth'});"
        " }; go(); })()</script>"
    )


@cache
def banner_html() -> str:
    """Inline <img> with alt text (``st.image`` has none and adds a fullscreen button)."""
    data = base64.b64encode(BANNER_PATH.read_bytes()).decode("ascii")
    return f'<img src="data:image/png;base64,{data}" alt="{BANNER_ALT}" style="width:100%;display:block">'
