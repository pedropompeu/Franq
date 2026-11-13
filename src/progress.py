"""Live progress of a question, for whoever is waiting on it.

A Gemini call takes seconds, and quota waits (rate limit, retry backoff) can take more. The agent
nodes and the quota wrappers call ``report(...)``; the caller of ``Assistant.ask`` decides what to
do with each event (the Streamlit app shows it; the evaluation ignores it).

The listener lives in a ``ContextVar``, so the LLM wrappers need no reference to the UI and two
Streamlit sessions sharing the same cached ``Assistant`` never see each other's events.
Exceptions raised by the listener propagate on purpose: Streamlit stops a script that the user
interrupted by raising from inside its own calls.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

ProgressKind = Literal[
    "context",  # reading the schema and the business dictionary
    "planning",  # first LLM call: understanding the question and writing the query
    "reviewing",  # LLM call after a successful query: reading the result
    "fixing",  # LLM call after a failed query: rewriting it
    "query_done",  # the query ran: ``rows`` and ``elapsed_ms``
    "query_failed",  # the database rejected the query
    "waiting",  # quota wait: ``seconds`` and ``reason``
    "resumed",  # the quota wait is over; the interrupted stage goes on
    "fallback",  # the current model is unavailable; ``model`` is the next one
]
WaitReason = Literal["rate_limit", "retry"]


@dataclass(frozen=True)
class Progress:
    kind: ProgressKind
    rows: int = 0
    elapsed_ms: float = 0.0
    seconds: float = 0.0
    reason: WaitReason = "retry"
    model: str = ""


Listener = Callable[[Progress], None]

_listener: ContextVar[Listener | None] = ContextVar("indaga_progress_listener", default=None)


def report(event: Progress) -> None:
    """Send ``event`` to the current listener, if any."""
    listener = _listener.get()
    if listener is not None:
        listener(event)


@contextmanager
def listening(listener: Listener | None) -> Iterator[None]:
    """Route ``report`` calls made inside the block to ``listener`` (``None`` keeps the current one)."""
    if listener is None:
        yield
        return
    token = _listener.set(listener)
    try:
        yield
    finally:
        _listener.reset(token)
