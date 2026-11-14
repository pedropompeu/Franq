"""Quota handling: cache, replay, rate limiter, retry/backoff, daily-quota memory and fallback. No network."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda

from src.agent.models import AgentDecision, TokenUsage
from src.llm import (
    GeminiLLM,
    LLMError,
    LLMReply,
    RateLimitError,
    TransientLLMError,
    classify_error,
    get_llm,
    parse_model_specs,
)
from src.resilience import (
    PACIFIC,
    CachedLLM,
    CacheMiss,
    FallbackLLM,
    QuotaStore,
    RateLimiter,
    ResilientLLM,
    equal_jitter,
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


def test_raw_provider_text_is_logged_not_returned(caplog: pytest.LogCaptureFixture) -> None:
    """The message reaches steps, the API and the screen; the provider's text stays in the log (feedback 2.8)."""
    err = _ApiError(400, "INVALID_ARGUMENT: API key AIzaSyEXEMPLO not valid; prompt '[nome_3] ...'")
    with caplog.at_level("WARNING", logger="src.llm"):
        out = classify_error(err, "m")

    assert "AIza" not in str(out) and "nome_3" not in str(out)
    assert str(out) == "m: erro do provedor (HTTP 400)"
    assert "AIzaSyEXEMPLO" in caplog.text


def test_schema_error_detail_is_logged_not_returned(caplog: pytest.LogCaptureFixture) -> None:
    broken = {"raw": AIMessage(content="{"), "parsed": None, "parsing_error": ValueError("trecho '[email_2]'")}
    with caplog.at_level("WARNING", logger="src.llm"), pytest.raises(TransientLLMError) as raised:
        _gemini_returning(broken).decide(MESSAGES)

    assert "email_2" not in str(raised.value) and "email_2" in caplog.text


def _gemini_returning(*outputs: Mapping[str, object]) -> GeminiLLM:
    """A GeminiLLM whose provider call is replaced by scripted outputs (no SDK client, no network)."""
    script = list(outputs)

    def reply(_: Any) -> dict[Any, Any]:
        return dict(script.pop(0))

    llm = GeminiLLM.__new__(GeminiLLM)
    llm._model = "m"
    llm._runnable = RunnableLambda(reply)
    return llm


def test_empty_gemini_response_is_transient_and_retried() -> None:
    empty = {"raw": AIMessage(content=""), "parsed": None, "parsing_error": None}
    good = {"raw": AIMessage(content="{}"), "parsed": DECISION, "parsing_error": None}
    with pytest.raises(TransientLLMError, match="resposta vazia"):
        _gemini_returning(empty).decide(MESSAGES)

    clock = FakeClock()
    limiter = RateLimiter(100, clock=clock)
    retried = ResilientLLM(
        _gemini_returning(empty, good), rpm=100, sleep=clock.sleep, limiter=limiter, jitter=lambda d: d
    )
    assert retried.decide(MESSAGES).decision == DECISION and clock.sleeps == [2.0]


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
    limiter = RateLimiter(100, clock=clock)
    return ResilientLLM(inner, rpm=100, quota=quota, sleep=clock.sleep, limiter=limiter, jitter=lambda d: d)


def test_transient_error_is_retried_with_backoff() -> None:
    clock = FakeClock()
    inner = ScriptedLLM("m", [TransientLLMError("503"), TransientLLMError("503")])
    reply = _resilient(inner, clock).decide(MESSAGES)
    assert reply.decision == DECISION and inner.calls == 3
    assert clock.sleeps == [2.0, 4.0]


def test_backoff_goes_through_the_jitter() -> None:
    clock = FakeClock()
    inner = ScriptedLLM("m", [TransientLLMError("503"), TransientLLMError("503")])
    limiter = RateLimiter(100, clock=clock)
    ResilientLLM(inner, rpm=100, sleep=clock.sleep, limiter=limiter, jitter=lambda d: d / 2).decide(MESSAGES)
    assert clock.sleeps == [1.0, 2.0]


def test_equal_jitter_stays_between_half_and_full_delay() -> None:
    samples = [equal_jitter(4.0) for _ in range(500)]
    assert all(2.0 <= s <= 4.0 for s in samples) and len(set(samples)) > 1


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


# ---------------------------------------------------------------- feedback 2026-11-14, 3.3, 3.6, 3.7


def test_429_needs_a_word_boundary_or_the_status() -> None:
    assert not isinstance(classify_error(ValueError("prompt de 1429 tokens inválido"), "m"), RateLimitError)
    assert isinstance(classify_error(ValueError("HTTP 429 Too Many Requests"), "m"), RateLimitError)
    assert isinstance(classify_error(_ApiError(429, "slow down"), "m"), RateLimitError)


def test_long_per_minute_wait_blocks_only_for_that_wait(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 11, 12, 15, 0, tzinfo=PACIFIC).timestamp())
    quota = QuotaStore(tmp_path / "quota.json", clock=clock)
    inner = ScriptedLLM("m", [RateLimitError("429", retry_after=600.0)])
    with pytest.raises(RateLimitError):
        _resilient(inner, clock, quota).decide(MESSAGES)
    until = quota.exhausted_until("m")
    assert until is not None and until == pytest.approx(clock.now + 600.0)
    clock.now += 601
    assert quota.exhausted_until("m") is None  # not until Pacific midnight


def test_quota_store_ignores_bad_entries_and_writes_atomically(tmp_path: Path) -> None:
    path = tmp_path / "quota.json"
    path.write_text('{"a": null, "b": "x", "c": [1]}', encoding="utf-8")
    store = QuotaStore(path, clock=lambda: 0.0)
    assert store.exhausted_until("a") is None and store.exhausted_until("c") is None
    store.mark_exhausted("m")
    assert store.exhausted_until("m") is not None
    assert [p.name for p in tmp_path.iterdir()] == ["quota.json"]  # no temp file left behind
    path.write_text("[1, 2]", encoding="utf-8")  # not even an object
    assert QuotaStore(path, clock=lambda: 0.0).exhausted_until("m") is None


def test_cache_write_failure_keeps_the_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inner = ScriptedLLM("m")
    cached = CachedLLM(inner, tmp_path)

    def broken(*args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", broken)
    reply = cached.decide(MESSAGES)
    assert reply.decision == DECISION and not reply.cached


def test_cache_temp_files_are_unique_per_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    names: list[str] = []
    original = Path.write_text

    def spy(self: Path, *args: object, **kwargs: object) -> int:
        names.append(self.name)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy)
    CachedLLM(ScriptedLLM("m"), tmp_path / "a").decide(MESSAGES)
    CachedLLM(ScriptedLLM("m"), tmp_path / "b").decide(MESSAGES)
    assert len(names) == 2 and names[0] != names[1]


def test_rate_limiter_sleeps_without_holding_the_lock() -> None:
    clock = FakeClock()
    held: list[bool] = []
    limiter = RateLimiter(1, clock=clock)

    def sleep(seconds: float) -> None:
        held.append(limiter._lock.locked())
        clock.sleep(seconds)

    limiter._sleep = sleep
    limiter.acquire()
    limiter.acquire()
    assert held == [False]
