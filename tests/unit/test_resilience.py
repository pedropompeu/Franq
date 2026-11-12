"""Quota handling: cache, replay, rate limiter, retry/backoff, daily-quota memory and fallback. No network."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from src.agent.models import AgentDecision, TokenUsage
from src.llm import LLMError, LLMReply, RateLimitError, TransientLLMError, classify_error, get_llm, parse_model_specs
from src.resilience import (
    PACIFIC,
    CachedLLM,
    CacheMiss,
    FallbackLLM,
    QuotaStore,
    RateLimiter,
    ResilientLLM,
    next_quota_reset,
)

MESSAGES: list[BaseMessage] = [SystemMessage(content="sistema"), HumanMessage(content="pergunta")]
DECISION = AgentDecision(thought="t", action="run_query", sql="SELECT 1")


class ScriptedLLM:
    def __init__(self, model: str, script: Sequence[LLMReply | Exception] = ()) -> None:
        self._model, self.script, self.calls = model, list(script), 0

    @property
    def model_name(self) -> str:
        return self._model

    def decide(self, messages: Sequence[BaseMessage]) -> LLMReply:
        self.calls += 1
        item = self.script.pop(0) if self.script else LLMReply(DECISION, TokenUsage(input_tokens=1), self._model)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now, self.sleeps = start, list[float]()

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# ---------------------------------------------------------------- classification


class _ApiError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def test_classify_daily_quota() -> None:
    err = _ApiError(429, "RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    out = classify_error(err, "m")
    assert isinstance(out, RateLimitError) and out.daily


def test_classify_per_minute_with_retry_delay_in_cause_chain() -> None:
    try:
        try:
            raise _ApiError(429, "RESOURCE_EXHAUSTED ... 'retryDelay': '7s'")
        except _ApiError as inner:
            raise RuntimeError("wrapped by langchain") from inner
    except RuntimeError as outer:
        out = classify_error(outer, "m")
    assert isinstance(out, RateLimitError) and not out.daily and out.retry_after == 7.0


@pytest.mark.parametrize("err", [_ApiError(503, "x"), TimeoutError("read"), RuntimeError("503 UNAVAILABLE")])
def test_classify_transient(err: Exception) -> None:
    assert isinstance(classify_error(err, "m"), TransientLLMError)


def test_classify_other_is_plain_llm_error() -> None:
    out = classify_error(_ApiError(400, "INVALID_ARGUMENT"), "m")
    assert type(out) is LLMError


def test_parse_model_specs() -> None:
    assert parse_model_specs("a:15, b") == [("a", 15), ("b", 5)]
    with pytest.raises(LLMError):
        parse_model_specs(" , ")


# ---------------------------------------------------------------- rate limiter


def test_rate_limiter_waits_for_the_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        limiter.acquire()
    assert clock.sleeps == []
    limiter.acquire()
    assert len(clock.sleeps) == 1 and 60.0 <= clock.sleeps[0] < 60.1


# ---------------------------------------------------------------- retry / quota memory


def _resilient(inner: ScriptedLLM, clock: FakeClock, quota: QuotaStore | None = None) -> ResilientLLM:
    return ResilientLLM(inner, rpm=100, quota=quota, sleep=clock.sleep, limiter=RateLimiter(100, clock=clock))


def test_transient_error_is_retried_with_backoff() -> None:
    clock = FakeClock()
    inner = ScriptedLLM("m", [TransientLLMError("503"), TransientLLMError("503")])
    reply = _resilient(inner, clock).decide(MESSAGES)
    assert reply.decision == DECISION and inner.calls == 3
    assert clock.sleeps == [2.0, 4.0]


def test_per_minute_429_honours_retry_after() -> None:
    clock = FakeClock()
    inner = ScriptedLLM("m", [RateLimitError("429", retry_after=7.0)])
    _resilient(inner, clock).decide(MESSAGES)
    assert clock.sleeps == [7.0]


def test_gives_up_after_max_attempts() -> None:
    clock = FakeClock()
    inner = ScriptedLLM("m", [TransientLLMError("503")] * 3)
    with pytest.raises(TransientLLMError):
        _resilient(inner, clock).decide(MESSAGES)
    assert inner.calls == 3


def test_daily_quota_is_remembered_on_disk(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 11, 12, 15, 0, tzinfo=PACIFIC).timestamp())
    store_path = tmp_path / "quota.json"
    inner = ScriptedLLM("m", [RateLimitError("429", daily=True)])
    with pytest.raises(RateLimitError):
        _resilient(inner, clock, QuotaStore(store_path, clock=clock)).decide(MESSAGES)
    assert inner.calls == 1 and clock.sleeps == []

    # a new process reads the mark and does not spend a request
    second = ScriptedLLM("m")
    with pytest.raises(RateLimitError):
        _resilient(second, clock, QuotaStore(store_path, clock=clock)).decide(MESSAGES)
    assert second.calls == 0

    clock.now = datetime(2026, 11, 13, 0, 1, tzinfo=PACIFIC).timestamp()  # after the reset
    assert _resilient(second, clock, QuotaStore(store_path, clock=clock)).decide(MESSAGES).decision == DECISION


def test_next_quota_reset_is_pacific_midnight() -> None:
    now = datetime(2026, 11, 12, 23, 59, tzinfo=PACIFIC).timestamp()
    assert datetime.fromtimestamp(next_quota_reset(now), PACIFIC) == datetime(2026, 11, 13, tzinfo=PACIFIC)


# ---------------------------------------------------------------- fallback


def test_fallback_uses_next_model() -> None:
    primary = ScriptedLLM("a", [RateLimitError("a: 429", daily=True)])
    secondary = ScriptedLLM("b")
    reply = FallbackLLM([primary, secondary]).decide(MESSAGES)
    assert reply.model == "b" and primary.calls == secondary.calls == 1


def test_fallback_reports_every_failure() -> None:
    chain = [ScriptedLLM("a", [LLMError("a falhou")]), ScriptedLLM("b", [LLMError("b falhou")])]
    with pytest.raises(LLMError, match=r"a falhou.*b falhou"):
        FallbackLLM(chain).decide(MESSAGES)


# ---------------------------------------------------------------- cache


def test_cache_hit_skips_the_provider(tmp_path: Path) -> None:
    inner = ScriptedLLM("m")
    cached = CachedLLM(inner, tmp_path)
    first, second = cached.decide(MESSAGES), cached.decide(MESSAGES)
    assert inner.calls == 1
    assert not first.cached and second.cached
    assert second.decision == DECISION and second.usage.input_tokens == 1


def test_cache_key_depends_on_prompt_and_model(tmp_path: Path) -> None:
    inner = ScriptedLLM("m")
    CachedLLM(inner, tmp_path).decide(MESSAGES)
    CachedLLM(inner, tmp_path).decide([*MESSAGES, HumanMessage(content="outra")])
    CachedLLM(ScriptedLLM("outro-modelo"), tmp_path).decide(MESSAGES)
    assert inner.calls == 2
    assert len(list(tmp_path.rglob("*.json"))) == 3


def test_replay_never_calls_the_provider(tmp_path: Path) -> None:
    inner = ScriptedLLM("m")
    with pytest.raises(CacheMiss):
        CachedLLM(inner, tmp_path, mode="replay").decide(MESSAGES)
    assert inner.calls == 0
    CachedLLM(inner, tmp_path).decide(MESSAGES)
    assert CachedLLM(inner, tmp_path, mode="replay").decide(MESSAGES).cached
    assert inner.calls == 1


def test_corrupt_cache_entry_is_refreshed(tmp_path: Path) -> None:
    inner = ScriptedLLM("m")
    CachedLLM(inner, tmp_path).decide(MESSAGES)
    entry = next(tmp_path.rglob("*.json"))
    entry.write_text("{corrompido", encoding="utf-8")
    assert not CachedLLM(inner, tmp_path).decide(MESSAGES).cached
    assert inner.calls == 2


# ---------------------------------------------------------------- factory


def test_replay_mode_needs_no_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("LLM_CACHE", "replay")
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_MODELS", "a:15,b:15")
    llm = get_llm()
    with pytest.raises(LLMError, match="LLM_CACHE=replay"):
        llm.decide(MESSAGES)


def test_live_mode_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("LLM_CACHE", "on")
    with pytest.raises(LLMError, match="GOOGLE_API_KEY"):
        get_llm()
