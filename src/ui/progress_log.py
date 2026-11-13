"""What the user sees while the answer is built: the current stage and the stages already done.

Pure state machine over ``src.progress.Progress`` events (no Streamlit import, tested). The app
feeds each event to ``ProgressLog.apply`` and repaints ``label`` (the ``st.status`` title, with a
spinner) and ``done`` (a checklist inside it).
"""

from __future__ import annotations

import math

from src.progress import Progress
from src.ui import texts
from src.ui.view import format_number

_STAGES = frozenset(texts.PROGRESS_DOING)


def _seconds(value: float) -> str:
    """Whole seconds, never "0 s" for a real wait."""
    return format_number(max(1, math.ceil(value)))


def _ms(value: float) -> str:
    return format_number(round(value, 1) if value < 10 else round(value))


def _rows(n: int) -> str:
    return f"{format_number(n)} {'linha' if n == 1 else 'linhas'}"


class ProgressLog:
    def __init__(self) -> None:
        self.label: str = texts.PROGRESS_START
        self.done: list[str] = []
        self._stage: str | None = None  # the stage in progress, closed when the next one starts
        self._wait: str | None = None  # line to log once the quota wait is over

    def apply(self, event: Progress) -> None:
        kind = event.kind
        if kind in _STAGES:
            self._close_stage()
            self._stage = kind
            self.label = texts.PROGRESS_DOING[kind]
        elif kind == "query_done":
            self._close_stage()
            self.done.append(texts.PROGRESS_QUERY_DONE.format(rows=_rows(event.rows), ms=_ms(event.elapsed_ms)))
        elif kind == "query_failed":
            self._close_stage()
            self.done.append(texts.PROGRESS_QUERY_FAILED)
        elif kind == "waiting":
            seconds = _seconds(event.seconds)
            self.label = texts.PROGRESS_WAIT[event.reason].format(seconds=seconds)
            self._wait = texts.PROGRESS_WAITED[event.reason].format(seconds=seconds)
        elif kind == "resumed":
            if self._wait is not None:
                self.done.append(self._wait)
                self._wait = None
            self.label = texts.PROGRESS_DOING[self._stage] if self._stage else texts.PROGRESS_START
        elif kind == "fallback":
            self.done.append(texts.PROGRESS_FALLBACK.format(model=event.model))

    def _close_stage(self) -> None:
        if self._stage is not None:
            self.done.append(texts.PROGRESS_DONE[self._stage])
            self._stage = None
