"""Demo cache warmer: each demo conversation runs in its own thread, in the order the screen asks it."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from src.agent.graph import Assistant
from src.config import Settings
from src.demo_cache import DELETE_REQUEST, FOLLOW_UP, demo_conversations, run
from src.llm import LLMError
from src.ui import texts
from tests.unit.test_agent import CHAT_SQL, P4_SQL, FakeLLM, turn


@pytest.fixture
def settings(db_path: Path, tmp_path: Path) -> Settings:
    return dataclasses.replace(Settings.from_env(), db_path=db_path, trace_path=tmp_path / "traces.jsonl")


def test_demo_conversations_follow_the_screen() -> None:
    conversations = demo_conversations()
    ranking, whatsapp, average, by_channel, trend = texts.SUGGESTED_QUESTIONS

    assert conversations[0] == (trend, DELETE_REQUEST)  # Tendência, then the write request
    assert (by_channel, FOLLOW_UP) in conversations
    assert {(ranking,), (whatsapp,), (average,), (DELETE_REQUEST,)} <= set(conversations)


def test_questions_of_a_conversation_share_its_memory(settings: Settings) -> None:
    llm = FakeLLM([*turn(P4_SQL, "Por canal."), *turn(CHAT_SQL, "No Chat."), *turn(P4_SQL, "De novo.")])
    lines: list[str] = []

    failures = run(
        Assistant(llm, settings), [("Quantos tickets por canal?", "E só no Chat?"), ("Outra?",)], lines.append
    )

    assert failures == 0
    assert "Pergunta: Quantos tickets por canal?" in llm.prompts[2]  # the follow-up's planning call
    assert "# Conversa anterior" not in llm.prompts[4]  # a new conversation starts empty
    assert [line.split("] ")[0] for line in lines] == ["[answered", "[answered", "[answered"]
    assert lines[1].endswith("E só no Chat?")


def test_a_question_out_of_the_cache_counts_as_failure(settings: Settings) -> None:
    llm = FakeLLM([LLMError("fora do cache"), *turn(P4_SQL, "Por canal.")])

    failures = run(Assistant(llm, settings), [("Primeira?",), ("Segunda?",)], lambda _: None)

    assert failures == 1


def test_published_demo_cache_answers_every_demo_question(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The "sem chave" path: ``demo/llm-cache`` replays the whole demo without the Gemini API.

    Failing here means the prompt changed: run ``LLM_CACHE=on make demo-cache``, audit the new
    files for personal data and copy them to ``demo/llm-cache``.
    """
    from src.config import PROJECT_ROOT
    from src.llm import get_llm

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_CACHE", "replay")
    monkeypatch.setenv("LLM_CACHE_DIR", str(PROJECT_ROOT / "demo" / "llm-cache"))
    for name in ("GEMINI_MODELS", "GEMINI_MODEL", "GOOGLE_API_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = dataclasses.replace(Settings.from_env(), trace_path=tmp_path / "traces.jsonl")
    lines: list[str] = []

    failures = run(Assistant(get_llm(), settings), demo_conversations(), lines.append)

    assert failures == 0, lines
    assert len(lines) == sum(len(c) for c in demo_conversations())
