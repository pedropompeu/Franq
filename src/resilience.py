"""Quota-aware wrappers around a ``StructuredLLM``.

Composition built by ``get_llm()`` (outermost first)::

    FallbackLLM([CachedLLM(ResilientLLM(GeminiLLM(model_a))), CachedLLM(ResilientLLM(GeminiLLM(model_b)))])

- ``CachedLLM``: identical prompt → stored decision, zero API calls (temperature 0 makes replay valid).
- ``ResilientLLM``: client-side RPM limiter, backoff with jitter on transient errors (including
  empty responses) and per-minute 429s, and a persisted "daily quota exhausted" mark so later
  runs skip the model until reset.
- ``FallbackLLM``: Gemini quotas are per model, so the next model gets a fresh budget.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from langchain_core.messages import BaseMessage

from src.agent.models import AgentDecision, TokenUsage
from src.llm import LLMError, LLMReply, RateLimitError, StructuredLLM, TransientLLMError
from src.progress import Progress, report

PACIFIC = ZoneInfo("America/Los_Angeles")  # Gemini daily quotas reset at midnight Pacific time
log = logging.getLogger(__name__)


class CacheMiss(LLMError):
    """Replay mode found no stored answer for this prompt."""


def equal_jitter(delay: float) -> float:
    """Random delay in ``[delay / 2, delay]``: spreads retries out while keeping a minimum wait."""
    return delay / 2 + random.uniform(0, delay / 2)  # noqa: S311 - timing, not cryptography


def _write_atomic(path: Path, text: str) -> None:
    """Write through a uniquely named temp file, so readers and other threads never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def next_quota_reset(now: float) -> float:
    local = datetime.fromtimestamp(now, PACIFIC)
    return datetime.combine((local + timedelta(days=1)).date(), dtime(0), PACIFIC).timestamp()


class RateLimiter:
    """Sliding one-minute window: blocks until a new request fits under ``rpm``."""

    def __init__(
        self,
        rpm: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rpm = rpm
        self._clock, self._sleep = clock, sleep
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Sleeps outside the lock, so every waiting caller reports its own wait (feedback 3.7)."""
        while True:
            with self._lock:
                now = self._clock()
                while self._sent and now - self._sent[0] >= 60.0:
                    self._sent.popleft()
                if len(self._sent) < self.rpm:
                    self._sent.append(now)
                    return
                wait = 60.0 - (now - self._sent[0]) + 0.05
            report(Progress("waiting", seconds=wait, reason="rate_limit"))
            self._sleep(wait)
            report(Progress("resumed"))


class QuotaStore:
    """Remembers, on disk, which models are out of quota and until when (daily, or a long retry)."""

    def __init__(self, path: Path | None, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self._clock = path, clock
        self._memory: dict[str, float] = {}
        self._lock = threading.Lock()

    def _load(self) -> dict[str, float]:
        if self.path is None or not self.path.is_file():
            return dict(self._memory)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return dict(self._memory)
        if not isinstance(raw, dict):
            return dict(self._memory)
        return {str(k): float(v) for k, v in raw.items() if isinstance(v, int | float) and not isinstance(v, bool)}

    def exhausted_until(self, model: str) -> float | None:
        until = self._load().get(model)
        return until if until is not None and until > self._clock() else None

    def mark_exhausted(self, model: str, *, seconds: float | None = None) -> None:
        """Until the next daily reset, or for ``seconds`` when the provider asked for a long wait."""
        with self._lock:
            data = self._load()
            now = self._clock()
            data[model] = now + seconds if seconds is not None else next_quota_reset(now)
            self._memory = data
            if self.path is not None:
                try:
                    _write_atomic(self.path, json.dumps(data, indent=2))
                except OSError as err:  # the mark stays in memory; losing it costs one request later
                    log.warning("quota store not saved: %s", err)


class ResilientLLM:
    def __init__(
        self,
        inner: StructuredLLM,
        *,
        rpm: int,
        quota: QuotaStore | None = None,
        max_attempts: int = 3,
        base_delay_s: float = 2.0,
        max_delay_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        limiter: RateLimiter | None = None,
        jitter: Callable[[float], float] = equal_jitter,
    ) -> None:
        self.inner = inner
        self._jitter = jitter
        self.quota = quota
        self.max_attempts = max_attempts
        self.base_delay_s, self.max_delay_s = base_delay_s, max_delay_s
        self._sleep = sleep
        self.limiter = limiter or RateLimiter(rpm, sleep=sleep)

    @property
    def model_name(self) -> str:
        return self.inner.model_name

    def decide(self, messages: Sequence[BaseMessage]) -> LLMReply:
        model = self.model_name
        if self.quota is not None and self.quota.exhausted_until(model) is not None:
            raise RateLimitError(f"{model}: cota esgotada por enquanto", daily=True)
        last: LLMError | None = None
        for attempt in range(1, self.max_attempts + 1):
            self.limiter.acquire()
            try:
                return self.inner.decide(messages)
            except RateLimitError as err:
                if err.daily:
                    if self.quota is not None:
                        self.quota.mark_exhausted(model)
                    raise
                if err.retry_after is not None and err.retry_after > self.max_delay_s:
                    # a long per-minute wait blocks the model for that wait only, not the whole day (3.6)
                    if self.quota is not None:
                        self.quota.mark_exhausted(model, seconds=err.retry_after)
                    raise
                # the server's retryDelay is honoured as given; only computed backoff gets jitter
                last, delay = err, err.retry_after or self._backoff(attempt)
            except TransientLLMError as err:
                last, delay = err, self._backoff(attempt)
            if attempt < self.max_attempts:
                wait = min(delay, self.max_delay_s)
                report(Progress("waiting", seconds=wait, reason="retry"))
                self._sleep(wait)
                report(Progress("resumed"))
        raise last or LLMError(f"{model}: nenhuma tentativa realizada")

    def _backoff(self, attempt: int) -> float:
        return self._jitter(self.base_delay_s * 2 ** (attempt - 1))


class FallbackLLM:
    """Tries each model in order; any ``LLMError`` moves on to the next one."""

    def __init__(self, chain: Sequence[StructuredLLM]) -> None:
        if not chain:
            raise ValueError("FallbackLLM needs at least one model")
        self.chain = list(chain)

    @property
    def model_name(self) -> str:
        return self.chain[0].model_name

    def decide(self, messages: Sequence[BaseMessage]) -> LLMReply:
        errors: list[str] = []
        for position, llm in enumerate(self.chain):
            if position > 0:
                report(Progress("fallback", model=llm.model_name))
            try:
                return llm.decide(messages)
            except LLMError as err:
                errors.append(str(err))
        raise LLMError("nenhum modelo disponível: " + " | ".join(errors))


def prompt_key(model: str, messages: Sequence[BaseMessage]) -> str:
    payload = {
        "model": model,
        "schema": AgentDecision.model_json_schema(),
        "messages": [[m.type, m.content] for m in messages],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class CachedLLM:
    """Disk cache keyed by model + full prompt + output schema.

    ``mode="on"`` reads and writes; ``mode="replay"`` only reads and raises ``CacheMiss``
    instead of calling the provider (develop the UI or rerun tests with zero quota).
    Stored decisions may quote query results, so the cache lives in the git-ignored ``.cache/``.
    """

    def __init__(self, inner: StructuredLLM, cache_dir: Path, *, mode: Literal["on", "replay"] = "on") -> None:
        self.inner, self.cache_dir, self.mode = inner, cache_dir, mode

    @property
    def model_name(self) -> str:
        return self.inner.model_name

    def _path(self, messages: Sequence[BaseMessage]) -> Path:
        return self.cache_dir / self.model_name / f"{prompt_key(self.model_name, messages)}.json"

    def decide(self, messages: Sequence[BaseMessage]) -> LLMReply:
        path = self._path(messages)
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return LLMReply(
                    decision=AgentDecision.model_validate(data["decision"]),
                    usage=TokenUsage.model_validate(data["usage"]),
                    model=str(data["model"]),
                    cached=True,
                )
            except (OSError, ValueError, KeyError):
                pass  # corrupt entry: fall through and refresh it
        if self.mode == "replay":
            raise CacheMiss(f"{self.model_name}: pergunta fora do cache (LLM_CACHE=replay)")
        reply = self.inner.decide(messages)
        payload = {"decision": reply.decision.model_dump(), "usage": reply.usage.model_dump(), "model": reply.model}
        try:
            _write_atomic(path, json.dumps(payload, ensure_ascii=False))
        except OSError as err:  # a full or read-only disk must not throw away an answer already paid for
            log.warning("LLM cache entry not saved: %s", err)
        return replace(reply, cached=False)


class OfflineLLM:
    """Placeholder provider for replay mode: never touches the network."""

    def __init__(self, model: str) -> None:
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    def decide(self, messages: Sequence[BaseMessage]) -> LLMReply:
        raise CacheMiss(f"{self._model}: modo offline, sem chamada à API")
