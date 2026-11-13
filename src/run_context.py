"""The run in progress, visible to the LLM layer without threading it through every call.

``Assistant.ask`` opens a ``run_scope`` around the graph; wrappers around the LLM (tracing,
LangFuse) read ``current_run()`` to tag their records with the trace id and the prompt version.
Like ``src/progress.py``, it lives in a ``ContextVar``, so concurrent sessions never mix runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RunInfo:
    trace_id: str
    prompt_version: str
    thread_id: str | None = None


_current: ContextVar[RunInfo | None] = ContextVar("indaga_current_run", default=None)


def current_run() -> RunInfo | None:
    """The run whose ``ask`` is executing in this context, if any."""
    return _current.get()


@contextmanager
def run_scope(info: RunInfo) -> Iterator[None]:
    """Make ``info`` the current run inside the block."""
    token = _current.set(info)
    try:
        yield
    finally:
        _current.reset(token)
