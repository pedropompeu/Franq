"""Prompt version (hash of everything that shapes the prompt) and the current-run context."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage

from src.agent.graph import Assistant
from src.config import Settings
from src.llm import LLMReply
from src.observability import Tracer
from src.run_context import RunInfo, current_run, run_scope
from src.versioning import prompt_version
from tests.unit.test_agent import P2_SQL, FakeLLM, answer, query


@pytest.fixture
def settings(db_path: Path, tmp_path: Path) -> Settings:
    return dataclasses.replace(Settings.from_env(), db_path=db_path, trace_path=tmp_path / "traces.jsonl")


def test_prompt_version_is_stable(settings: Settings) -> None:
    first = prompt_version(settings.dictionary_path)
    assert re.fullmatch(r"[0-9a-f]{8}", first)
    assert prompt_version(settings.dictionary_path) == first


def test_prompt_version_changes_with_dictionary(settings: Settings, tmp_path: Path) -> None:
    copy = tmp_path / "dictionary.yaml"
    copy.write_text(settings.dictionary_path.read_text(encoding="utf-8") + "\n# outra linha\n", encoding="utf-8")
    assert prompt_version(copy) != prompt_version(settings.dictionary_path)


def test_run_scope_is_visible_inside_and_cleared_after() -> None:
    info = RunInfo(trace_id="abc", prompt_version="12345678")
    assert current_run() is None
    with run_scope(info):
        assert current_run() == info
    assert current_run() is None


def test_response_and_trace_carry_prompt_version(settings: Settings) -> None:
    tracer = Tracer(settings.trace_path)
    assistant = Assistant(FakeLLM([query(P2_SQL), answer(P2_SQL, "17.")]), settings, tracer=tracer)
    response = assistant.ask("WhatsApp 2024?")
    assert response.prompt_version == assistant.prompt_version == prompt_version(settings.dictionary_path)
    [record] = tracer.tail(1)
    assert record["prompt_version"] == response.prompt_version


class RunSpyLLM(FakeLLM):
    def __init__(self, script: Sequence[object]) -> None:
        super().__init__(script)  # type: ignore[arg-type]
        self.seen: list[RunInfo | None] = []

    def decide(self, messages: Sequence[BaseMessage]) -> LLMReply:
        self.seen.append(current_run())
        return super().decide(messages)


def test_llm_sees_run_info_during_ask(settings: Settings) -> None:
    llm = RunSpyLLM([query(P2_SQL), answer(P2_SQL, "17.")])
    assistant = Assistant(llm, settings)
    response = assistant.ask("WhatsApp 2024?")
    assert len(llm.seen) == 2
    for info in llm.seen:
        assert info is not None
        assert info.trace_id == response.trace_id
        assert info.prompt_version == assistant.prompt_version
        assert info.thread_id is None
    assert current_run() is None
