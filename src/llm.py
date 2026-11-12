"""LLM provider layer. The graph depends only on the ``StructuredLLM`` protocol.

Resilience (retry with backoff, fallback model, circuit breaker, rate limiting) is added
on Day 4 inside this module, without touching the graph.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

from src.agent.models import AgentDecision, TokenUsage


class LLMError(Exception):
    """Any failure to obtain a valid structured decision from the provider."""


@dataclass(frozen=True)
class LLMReply:
    decision: AgentDecision
    usage: TokenUsage
    model: str


class StructuredLLM(Protocol):
    @property
    def model_name(self) -> str: ...

    def decide(self, messages: Sequence[BaseMessage]) -> LLMReply: ...


def _usage_from(raw: Any) -> TokenUsage:
    meta = getattr(raw, "usage_metadata", None) if isinstance(raw, AIMessage) else None
    if not meta:
        return TokenUsage()
    return TokenUsage(input_tokens=int(meta.get("input_tokens", 0)), output_tokens=int(meta.get("output_tokens", 0)))


class GeminiLLM:
    """Gemini through langchain-google-genai, temperature 0, JSON-schema structured output."""

    def __init__(self, model: str, api_key: str, timeout_s: float = 60.0) -> None:
        from langchain_google_genai import ChatGoogleGenerativeAI

        self._model = model
        chat = ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            temperature=0,
            timeout=timeout_s,
            max_retries=0,  # retries are owned by this module (Day 4), not hidden in the SDK
        )
        self._runnable = chat.with_structured_output(AgentDecision, include_raw=True)

    @property
    def model_name(self) -> str:
        return self._model

    def decide(self, messages: Sequence[BaseMessage]) -> LLMReply:
        try:
            out = self._runnable.invoke(list(messages))
        except Exception as err:  # provider/network errors surface as LLMError
            raise LLMError(f"{type(err).__name__}: {err}") from err
        if not isinstance(out, dict):
            raise LLMError("resposta inesperada do provedor")
        parsed = out.get("parsed")
        if out.get("parsing_error") is not None or not isinstance(parsed, BaseModel):
            raise LLMError(f"resposta vazia ou fora do schema: {out.get('parsing_error')}")
        return LLMReply(
            decision=AgentDecision.model_validate(parsed.model_dump()),
            usage=_usage_from(out.get("raw")),
            model=self._model,
        )


def get_llm() -> StructuredLLM:
    """Build the provider selected by ``LLM_PROVIDER`` (default: gemini)."""
    provider = os.getenv("LLM_PROVIDER", "gemini").lower()
    if provider == "gemini":
        api_key = os.getenv("GOOGLE_API_KEY", "")
        if not api_key:
            raise LLMError("GOOGLE_API_KEY não definida. Copie .env.example para .env e preencha a chave.")
        return GeminiLLM(model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), api_key=api_key)
    raise LLMError(f"LLM_PROVIDER desconhecido: {provider!r}")
