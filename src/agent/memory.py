"""Conversation memory: the masked turns of each ``thread_id``, and nothing else (ADR 0008).

Replaces LangGraph's ``InMemorySaver``, whose checkpoints kept every intermediate state of a turn,
result rows included, for as long as the process lived (feedback 2026-11-14, 3.2). Here a thread
holds only ``Turn`` objects (masked question, SQL and answer), and the store is bounded three ways:

- ``max_threads``: least recently used conversations are dropped first (LRU);
- ``ttl_s``: a conversation idle for longer than this is forgotten;
- ``max_turns``: a long conversation keeps only its last turns (the prompt reads three).

``hold(thread_id)`` serializes questions of the same conversation, so two concurrent requests
cannot both read the same history and interleave their turns (feedback 3.1). A held conversation
is never evicted.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from src.agent.models import Turn

MAX_THREADS = 500
TTL_S = 4 * 60 * 60
MAX_TURNS = 20


@dataclass
class _Thread:
    turns: list[Turn] = field(default_factory=list)
    last_used: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    holders: int = 0  # requests waiting for or holding ``lock``


class ConversationStore:
    def __init__(
        self,
        *,
        max_threads: int = MAX_THREADS,
        ttl_s: float = TTL_S,
        max_turns: int = MAX_TURNS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_threads = max_threads
        self.ttl_s = ttl_s
        self.max_turns = max_turns
        self._clock = clock
        self._threads: OrderedDict[str, _Thread] = OrderedDict()
        self._guard = threading.Lock()

    def __len__(self) -> int:
        with self._guard:
            self._expire()
            return len(self._threads)

    # ---------------------------------------------------------------- internals (guard held)

    def _expire(self) -> None:
        cutoff = self._clock() - self.ttl_s
        for key in [k for k, th in self._threads.items() if th.last_used < cutoff and not th.holders]:
            del self._threads[key]

    def _find(self, thread_id: str) -> _Thread | None:
        self._expire()
        thread = self._threads.get(thread_id)
        if thread is not None:
            thread.last_used = self._clock()
            self._threads.move_to_end(thread_id)
        return thread

    def _ensure(self, thread_id: str) -> _Thread:
        thread = self._find(thread_id)
        if thread is None:
            thread = self._threads[thread_id] = _Thread(last_used=self._clock())
            self._evict(keep=thread_id)
        return thread

    def _evict(self, *, keep: str) -> None:
        excess = len(self._threads) - self.max_threads
        for key in [k for k, th in self._threads.items() if k != keep and not th.holders]:
            if excess <= 0:
                break
            del self._threads[key]
            excess -= 1

    # ------------------------------------------------------------------------------- API

    def turns(self, thread_id: str) -> list[Turn]:
        """The kept turns of ``thread_id``, oldest first; empty when unknown or expired."""
        with self._guard:
            thread = self._find(thread_id)
            return list(thread.turns) if thread is not None else []

    def append(self, thread_id: str, turn: Turn) -> None:
        with self._guard:
            thread = self._ensure(thread_id)
            thread.turns = [*thread.turns, turn][-self.max_turns :]

    @contextmanager
    def hold(self, thread_id: str) -> Iterator[None]:
        """One question at a time per conversation; the conversation is not evicted meanwhile."""
        with self._guard:
            thread = self._ensure(thread_id)
            thread.holders += 1
        try:
            with thread.lock:
                yield
        finally:
            with self._guard:
                thread.holders -= 1
                thread.last_used = self._clock()
