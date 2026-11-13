"""Provider selection: Gemini through AI Studio (API key) or Vertex AI (Application Default Credentials)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import langchain_google_genai
import pytest

from src.llm import GeminiLLM, LLMError, VertexTarget, get_llm


class FakeChat:
    """Stands in for ChatGoogleGenerativeAI: records the constructor kwargs, never calls the network."""

    calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        FakeChat.calls.append(kwargs)

    def with_structured_output(self, *_args: Any, **_kwargs: Any) -> FakeChat:
        return self


@pytest.fixture
def fake_chat(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    FakeChat.calls = []
    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", FakeChat)
    return FakeChat.calls


@pytest.fixture
def vertex_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    monkeypatch.setenv("LLM_PROVIDER", "vertex")
    monkeypatch.setenv("LLM_CACHE", "off")
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_MODELS", "gemini-x:15")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    return monkeypatch


def test_vertex_requires_project(vertex_env: pytest.MonkeyPatch, fake_chat: list[dict[str, Any]]) -> None:
    vertex_env.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(LLMError, match=r"GOOGLE_CLOUD_PROJECT não definida para LLM_PROVIDER=vertex\."):
        get_llm()
    assert fake_chat == []


def test_vertex_builds_chat_with_project_and_location(
    vertex_env: pytest.MonkeyPatch, fake_chat: list[dict[str, Any]]
) -> None:
    vertex_env.setenv("GOOGLE_CLOUD_PROJECT", "meu-projeto")
    llm = get_llm()  # no GOOGLE_API_KEY needed: ADC
    assert llm.model_name == "gemini-x"
    [kwargs] = fake_chat
    assert kwargs["vertexai"] is True
    assert kwargs["project"] == "meu-projeto"
    assert kwargs["location"] == "southamerica-east1"
    assert "google_api_key" not in kwargs


def test_vertex_location_comes_from_env(vertex_env: pytest.MonkeyPatch, fake_chat: list[dict[str, Any]]) -> None:
    vertex_env.setenv("GOOGLE_CLOUD_PROJECT", "meu-projeto")
    vertex_env.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    get_llm()
    assert fake_chat[0]["location"] == "us-central1"


def test_gemini_keeps_api_key_and_no_vertex(fake_chat: list[dict[str, Any]]) -> None:
    GeminiLLM("gemini-x", "chave")
    [kwargs] = fake_chat
    assert kwargs["google_api_key"] == "chave"
    assert "vertexai" not in kwargs and "project" not in kwargs
    GeminiLLM("gemini-x", vertex=VertexTarget(project="p", location="l"))
    assert fake_chat[1]["vertexai"] is True


def test_unknown_provider_still_rejected(vertex_env: pytest.MonkeyPatch) -> None:
    vertex_env.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(LLMError, match="LLM_PROVIDER desconhecido"):
        get_llm()


def test_vertex_ignores_ai_studio_quota_mark(
    vertex_env: pytest.MonkeyPatch, fake_chat: list[dict[str, Any]], tmp_path: Path
) -> None:
    from src.resilience import QuotaStore, ResilientLLM

    QuotaStore(tmp_path / "quota.json").mark_exhausted("gemini-x")  # AI Studio free tier ran out
    vertex_env.setenv("GOOGLE_CLOUD_PROJECT", "meu-projeto")
    llm = get_llm()
    assert isinstance(llm, ResilientLLM)
    assert llm.quota is not None
    assert llm.quota.exhausted_until("gemini-x") is None  # billed project: its own quota
