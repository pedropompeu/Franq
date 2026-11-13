"""Live progress: the listener channel, the events of the agent and quota layers, and what the UI shows."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from src.agent.graph import Assistant
from src.config import Settings
from src.llm import LLMError, RateLimitError, TransientLLMError
from src.progress import Progress, listening, report
from src.resilience import FallbackLLM, RateLimiter
from src.ui import texts
from src.ui.progress_log import ProgressLog
from tests.unit.test_agent import P2_SQL, FakeLLM, answer, query
from tests.unit.test_resilience import MESSAGES, FakeClock, ScriptedLLM, _resilient


@pytest.fixture
def settings(db_path: Path, tmp_path: Path) -> Settings:
    return dataclasses.replace(Settings.from_env(), db_path=db_path, trace_path=tmp_path / "traces.jsonl")


# ---------------------------------------------------------------- channel


def test_report_without_listener_is_a_no_op() -> None:
    report(Progress("context"))  # must not raise


def test_listening_routes_events_and_restores_the_previous_listener() -> None:
    outer: list[str] = []
    inner: list[str] = []
    with listening(lambda e: outer.append(e.kind)):
        report(Progress("context"))
        with listening(lambda e: inner.append(e.kind)):
            report(Progress("planning"))
        with listening(None):  # None keeps the current listener
            report(Progress("reviewing"))
    report(Progress("fixing"))
    assert outer == ["context", "reviewing"] and inner == ["planning"]


def test_listener_errors_propagate() -> None:
    """Streamlit interrupts a script by raising from its own calls; swallowing that would hang the UI."""

    def boom(_: Progress) -> None:
        raise RuntimeError("stop")

    with listening(boom), pytest.raises(RuntimeError, match="stop"):
        report(Progress("context"))


# ---------------------------------------------------------------- agent events


def test_agent_reports_each_stage_in_order(settings: Settings) -> None:
    events: list[Progress] = []
    llm = FakeLLM([query(P2_SQL), answer(P2_SQL, "17 clientes.")])
    Assistant(llm, settings).ask("WhatsApp 2024?", on_progress=events.append)

    assert [e.kind for e in events] == ["context", "planning", "query_done", "reviewing"]
    assert events[2].rows == 1 and events[2].elapsed_ms >= 0


def test_agent_reports_the_correction(settings: Settings) -> None:
    events: list[str] = []
    broken = "SELECT COUNT(*) FROM campanhas WHERE canal = 'WhatsApp'"
    llm = FakeLLM([query(broken), query(P2_SQL), answer(P2_SQL, "17.")])
    Assistant(llm, settings).ask("WhatsApp 2024?", on_progress=lambda e: events.append(e.kind))

    assert events == ["context", "planning", "query_failed", "fixing", "query_done", "reviewing"]


def test_ask_without_listener_still_answers(settings: Settings) -> None:
    llm = FakeLLM([query(P2_SQL), answer(P2_SQL, "17.")])
    assert Assistant(llm, settings).ask("WhatsApp 2024?").status == "answered"


# ---------------------------------------------------------------- quota events


def test_retry_wait_is_reported_before_and_after_the_sleep() -> None:
    clock = FakeClock()
    seen: list[tuple[str, float, int]] = []
    inner = ScriptedLLM("m", [TransientLLMError("503")])
    with listening(lambda e: seen.append((e.kind, e.seconds, len(clock.sleeps)))):
        _resilient(inner, clock).decide(MESSAGES)
    assert seen == [("waiting", 2.0, 0), ("resumed", 0.0, 1)]


def test_per_minute_429_reports_the_server_delay() -> None:
    clock = FakeClock()
    seen: list[Progress] = []
    with listening(seen.append):
        _resilient(ScriptedLLM("m", [RateLimitError("429", retry_after=7.0)]), clock).decide(MESSAGES)
    assert seen[0] == Progress("waiting", seconds=7.0, reason="retry")


def test_rate_limiter_reports_its_wait() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, clock=clock, sleep=clock.sleep)
    limiter.acquire()
    seen: list[Progress] = []
    with listening(seen.append):
        limiter.acquire()
    assert [e.kind for e in seen] == ["waiting", "resumed"]
    assert seen[0].reason == "rate_limit" and 60.0 <= seen[0].seconds < 60.1


def test_fallback_reports_the_next_model_only_when_it_is_used() -> None:
    seen: list[Progress] = []
    with listening(seen.append):
        FallbackLLM([ScriptedLLM("a"), ScriptedLLM("b")]).decide(MESSAGES)
        assert seen == []
        FallbackLLM([ScriptedLLM("a", [LLMError("a falhou")]), ScriptedLLM("b")]).decide(MESSAGES)
    assert seen == [Progress("fallback", model="b")]


# ---------------------------------------------------------------- what the UI shows


def test_log_shows_the_stage_in_progress_and_checks_the_finished_ones() -> None:
    log = ProgressLog()
    assert log.label == texts.PROGRESS_START and log.done == []

    log.apply(Progress("context"))
    log.apply(Progress("planning"))
    assert log.label == texts.PROGRESS_DOING["planning"]
    assert log.done == [texts.PROGRESS_DONE["context"]]

    log.apply(Progress("query_done", rows=5, elapsed_ms=3.24))
    log.apply(Progress("reviewing"))
    assert log.label == texts.PROGRESS_DOING["reviewing"]
    assert log.done == [
        texts.PROGRESS_DONE["context"],
        texts.PROGRESS_DONE["planning"],
        "Consulta executada no banco: 5 linhas em 3,2 ms",
    ]


def test_log_formats_one_row_and_large_numbers() -> None:
    log = ProgressLog()
    log.apply(Progress("query_done", rows=1, elapsed_ms=1234.4))
    assert log.done == ["Consulta executada no banco: 1 linha em 1.234 ms"]


def test_log_correction_flow() -> None:
    log = ProgressLog()
    for kind in ("planning", "query_failed", "fixing"):
        log.apply(Progress(kind))
    assert log.label == texts.PROGRESS_DOING["fixing"]
    assert log.done == [texts.PROGRESS_DONE["planning"], texts.PROGRESS_QUERY_FAILED]


def test_log_wait_takes_the_label_and_gives_it_back() -> None:
    log = ProgressLog()
    log.apply(Progress("planning"))
    log.apply(Progress("waiting", seconds=19.2, reason="rate_limit"))
    assert log.label == "Limite de pedidos por minuto do Gemini: aguardando cerca de 20 s…"
    assert log.done == []

    log.apply(Progress("resumed"))
    assert log.label == texts.PROGRESS_DOING["planning"]
    assert log.done == ["Aguardou o limite por minuto do Gemini (cerca de 20 s)"]


def test_log_never_promises_a_zero_second_wait() -> None:
    log = ProgressLog()
    log.apply(Progress("waiting", seconds=0.05, reason="retry"))
    assert "cerca de 1 s" in log.label


def test_log_fallback_names_the_model_and_keeps_the_label() -> None:
    log = ProgressLog()
    log.apply(Progress("planning"))
    log.apply(Progress("fallback", model="gemini-3.1-flash-lite"))
    assert log.label == texts.PROGRESS_DOING["planning"]
    assert log.done == ["Modelo principal indisponível; seguindo com gemini-3.1-flash-lite"]
