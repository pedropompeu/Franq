"""Conversation memory: masked turns per thread, bounded by count (LRU), idle time (TTL) and length."""

from __future__ import annotations

from src.agent.memory import ConversationStore
from src.agent.models import Turn


def t(n: int) -> Turn:
    return Turn(question=f"q{n}", sql="", answer=f"a{n}", status="answered")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_unknown_thread_is_empty() -> None:
    assert ConversationStore().turns("x") == []


def test_append_and_read_in_order() -> None:
    store = ConversationStore()
    store.append("a", t(1))
    store.append("a", t(2))
    assert [x.question for x in store.turns("a")] == ["q1", "q2"]
    assert len(store) == 1


def test_least_recently_used_thread_is_evicted() -> None:
    store = ConversationStore(max_threads=2)
    store.append("a", t(1))
    store.append("b", t(1))
    store.turns("a")  # a is used again: b is now the oldest
    store.append("c", t(1))
    assert store.turns("b") == [] and store.turns("a") and store.turns("c")
    assert len(store) == 2


def test_idle_thread_expires() -> None:
    clock = Clock()
    store = ConversationStore(ttl_s=60, clock=clock)
    store.append("a", t(1))
    clock.now = 59
    assert store.turns("a")
    clock.now = 59 + 61
    assert store.turns("a") == []
    assert len(store) == 0


def test_long_thread_keeps_only_the_last_turns() -> None:
    store = ConversationStore(max_turns=3)
    for n in range(1, 6):
        store.append("a", t(n))
    assert [x.question for x in store.turns("a")] == ["q3", "q4", "q5"]


def test_held_thread_is_not_evicted() -> None:
    store = ConversationStore(max_threads=1)
    store.append("a", t(1))
    with store.hold("a"):
        store.append("b", t(1))
        assert store.turns("a")
