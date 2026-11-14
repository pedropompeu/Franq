"""LLM provider layer. The graph depends only on the ``StructuredLLM`` protocol.

Quota handling (cache, rate limiting, retry, fallback across models) lives in
``src/resilience.py`` and is assembled by ``get_llm()``; the graph never sees it.
"""

from __future__ import annotations

import os
import re
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

from src.agent.models import AgentDecision, TokenUsage
from src.config import PROJECT_ROOT

# Free tier (AI Studio, checked 2026-11-12): both 15 RPM / 500 RPD, with separate quotas per model.
DEFAULT_GEMINI_MODELS = "gemini-3.5-flash-lite:15,gemini-3.1-flash-lite:15"
DEFAULT_RPM = 5  # conservative when a model is listed without ":rpm"


class LLMError(Exception):
    """Any failure to obtain a valid structured decision from the provider."""


class RateLimitError(LLMError):
    """HTTP 429. ``daily`` means the per-day quota is gone; otherwise wait ``retry_after`` seconds."""

    def __init__(self, message: str, *, daily: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.daily, self.retry_after = daily, retry_after


class TransientLLMError(LLMError):
    """Timeouts and 5xx: worth retrying with backoff."""


@dataclass(frozen=True)
class LLMReply:
    decision: AgentDecision
    usage: TokenUsage
    model: str
    cached: bool = False


class StructuredLLM(Protocol):
    @property
    def model_name(self) -> str: ...

    def decide(self, messages: Sequence[BaseMessage]) -> LLMReply: ...


def _usage_from(raw: Any) -> TokenUsage:
    meta = getattr(raw, "usage_metadata", None) if isinstance(raw, AIMessage) else None
    if not meta:
        return TokenUsage()
    return TokenUsage(input_tokens=int(meta.get("input_tokens", 0)), output_tokens=int(meta.get("output_tokens", 0)))


def _chain(err: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = err
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


_HTTP_429 = re.compile(r"(?<![\w.])429(?![\w.])")  # "1429 tokens" is not a rate limit (feedback 3.6)
_RETRY_RE = re.compile(r"retry(?:Delay)?[^0-9]{0,8}(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_TRANSIENT_MARKERS = ("UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL", "timed out", "Timeout", "overloaded")


def classify_error(err: BaseException, model: str) -> LLMError:
    """Map provider exceptions to retry semantics without depending on SDK exception classes."""
    chain = list(_chain(err))
    text = " ".join(f"{type(e).__name__}: {e}" for e in chain)
    codes = {getattr(e, attr, None) for e in chain for attr in ("code", "status_code")}
    if 429 in codes or "RESOURCE_EXHAUSTED" in text or _HTTP_429.search(text):
        match = _RETRY_RE.search(text)
        return RateLimitError(
            f"{model}: limite de requisições (429)",
            daily="PerDay" in text,
            retry_after=float(match.group(1)) if match else None,
        )
    if codes & {500, 502, 503, 504} or any(isinstance(e, TimeoutError) for e in chain):
        return TransientLLMError(f"{model}: erro temporário do provedor")
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return TransientLLMError(f"{model}: erro temporário do provedor")
    return LLMError(f"{model}: {type(err).__name__}: {err}")


DEFAULT_VERTEX_LOCATION = "southamerica-east1"


@dataclass(frozen=True)
class VertexTarget:
    """Vertex AI project and region; authentication comes from Application Default Credentials."""

    project: str
    location: str = DEFAULT_VERTEX_LOCATION


class GeminiLLM:
    """Gemini through langchain-google-genai, temperature 0, JSON-schema structured output.

    AI Studio with ``api_key`` by default; Vertex AI when ``vertex`` is given (no key, ADC).
    """

    def __init__(
        self, model: str, api_key: str = "", timeout_s: float = 60.0, *, vertex: VertexTarget | None = None
    ) -> None:
        import langchain_google_genai

        self._model = model
        backend: dict[str, Any] = (
            {"vertexai": True, "project": vertex.project, "location": vertex.location}
            if vertex is not None
            else {"google_api_key": api_key}
        )
        chat = langchain_google_genai.ChatGoogleGenerativeAI(
            model=model,
            temperature=0,
            timeout=timeout_s,
            max_retries=0,  # retries are owned by ResilientLLM, not hidden in the SDK
            **backend,
        )
        self._runnable = chat.with_structured_output(AgentDecision, include_raw=True)

    @property
    def model_name(self) -> str:
        return self._model

    def decide(self, messages: Sequence[BaseMessage]) -> LLMReply:
        try:
            with warnings.catch_warnings():
                # Gemini 3.x Lite ignores temperature ("fixed sampling defaults"); the cache gives replayability.
                warnings.filterwarnings("ignore", message=".*fixed sampling defaults.*", category=UserWarning)
                out = self._runnable.invoke(list(messages))
        except Exception as err:  # provider/network errors surface as typed LLMErrors
            raise classify_error(err, self._model) from err
        if not isinstance(out, dict):
            raise LLMError(f"{self._model}: resposta inesperada do provedor")
        parsed = out.get("parsed")
        if out.get("parsing_error") is not None or not isinstance(parsed, BaseModel):
            # Gemini sometimes returns an empty candidate; the same prompt usually works on the next try.
            raise TransientLLMError(f"{self._model}: resposta vazia ou fora do schema: {out.get('parsing_error')}")
        return LLMReply(
            decision=AgentDecision.model_validate(parsed.model_dump()),
            usage=_usage_from(out.get("raw")),
            model=self._model,
        )


def parse_model_specs(raw: str) -> list[tuple[str, int]]:
    """``"model-a:15,model-b"`` → ``[("model-a", 15), ("model-b", DEFAULT_RPM)]``."""
    specs: list[tuple[str, int]] = []
    for item in raw.split(","):
        name, _, rpm = item.strip().partition(":")
        if name:
            specs.append((name.strip(), int(rpm) if rpm.strip() else DEFAULT_RPM))
    if not specs:
        raise LLMError("nenhum modelo configurado em GEMINI_MODELS")
    return specs


def get_llm() -> StructuredLLM:
    """Assemble the provider chain from the environment.

    - ``GEMINI_MODELS``: ordered fallback list, ``model[:rpm]`` separated by commas.
    - ``LLM_CACHE``: ``on`` (default), ``off`` or ``replay`` (cache only, never calls the API).
    - ``LLM_PROVIDER``: ``gemini`` (default, AI Studio, ``GOOGLE_API_KEY``) or ``vertex``
      (``GOOGLE_CLOUD_PROJECT``, ``GOOGLE_CLOUD_LOCATION``, Application Default Credentials).
    - ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` (optional): wraps the chain in
      :class:`src.langfuse_bridge.TracedLLM`, which records the already masked messages.
    """
    from src.resilience import CachedLLM, FallbackLLM, OfflineLLM, QuotaStore, ResilientLLM

    provider = os.getenv("LLM_PROVIDER", "gemini").lower()
    if provider not in ("gemini", "vertex"):
        raise LLMError(f"LLM_PROVIDER desconhecido: {provider!r}")
    specs = parse_model_specs(os.getenv("GEMINI_MODELS") or os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODELS)
    cache_mode = os.getenv("LLM_CACHE", "on").lower()
    if cache_mode not in ("on", "off", "replay"):
        raise LLMError(f"LLM_CACHE inválido: {cache_mode!r} (use on, off ou replay)")
    cache_dir = Path(os.getenv("LLM_CACHE_DIR", PROJECT_ROOT / ".cache" / "llm"))

    api_key = os.getenv("GOOGLE_API_KEY", "")
    vertex: VertexTarget | None = None
    if provider == "vertex" and cache_mode != "replay":
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "")
        if not project:
            raise LLMError("GOOGLE_CLOUD_PROJECT não definida para LLM_PROVIDER=vertex.")
        vertex = VertexTarget(project, os.getenv("GOOGLE_CLOUD_LOCATION") or DEFAULT_VERTEX_LOCATION)
    elif cache_mode != "replay" and not api_key:
        raise LLMError("GOOGLE_API_KEY não definida. Copie .env.example para .env e preencha a chave.")

    # one quota file per provider: an exhausted AI Studio free tier says nothing about a billed Vertex project
    quota = QuotaStore(cache_dir / ("quota.json" if provider == "gemini" else f"quota-{provider}.json"))
    chain: list[StructuredLLM] = []
    for model, rpm in specs:
        if cache_mode == "replay":
            chain.append(CachedLLM(OfflineLLM(model), cache_dir, mode="replay"))
            continue
        llm: StructuredLLM = ResilientLLM(GeminiLLM(model, api_key, vertex=vertex), rpm=rpm, quota=quota)
        chain.append(CachedLLM(llm, cache_dir) if cache_mode == "on" else llm)
    head = chain[0] if len(chain) == 1 else FallbackLLM(chain)
    from src import langfuse_bridge  # lazy: imports langfuse only when its keys are set

    sink = langfuse_bridge.langfuse_sink_from_env()
    return langfuse_bridge.TracedLLM(head, sink) if sink is not None else head
