"""HTTP API over the same Assistant: scripted LLM, real database, no network, no server process."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.agent.graph import Assistant
from src.api import create_app
from src.config import Settings
from tests.unit.test_agent import CHAT_SQL, P2_SQL, P4_SQL, FakeLLM, answer, query, turn


@pytest.fixture
def settings(db_path: Path, tmp_path: Path) -> Settings:
    return dataclasses.replace(Settings.from_env(), db_path=db_path, trace_path=tmp_path / "traces.jsonl")


def p2_assistant(settings: Settings) -> Assistant:
    llm = FakeLLM([query(P2_SQL), answer(P2_SQL, "17 clientes.", chart_type="metric", y_column="clientes")])
    return Assistant(llm, settings)


def sse_events(body: str) -> list[tuple[str, dict[str, object]]]:
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def test_health(settings: Settings) -> None:
    assistant = p2_assistant(settings)
    reply = TestClient(create_app(assistant)).get("/health")

    assert reply.status_code == 200
    assert reply.json() == {"status": "ok", "model": "fake-llm", "prompt_version": assistant.prompt_version}


def test_ask_returns_agent_response(settings: Settings) -> None:
    reply = TestClient(create_app(p2_assistant(settings))).post("/ask", json={"question": "WhatsApp em 2024?"})

    assert reply.status_code == 200
    body = reply.json()
    assert body["status"] == "answered" and body["answer"] == "17 clientes."
    assert body["data"]["rows"] == [[17]] and body["trace_id"]


@pytest.mark.parametrize("question", ["", "x" * 501])
def test_ask_validates_question(settings: Settings, question: str) -> None:
    reply = TestClient(create_app(p2_assistant(settings))).post("/ask", json={"question": question})
    assert reply.status_code == 422


def test_ask_validates_thread_id(settings: Settings) -> None:
    client = TestClient(create_app(p2_assistant(settings)))
    reply = client.post("/ask", json={"question": "?", "thread_id": "../etc"})
    assert reply.status_code == 422


def test_stream_emits_progress_then_answer(settings: Settings) -> None:
    client = TestClient(create_app(p2_assistant(settings)))
    with client.stream("POST", "/ask/stream", json={"question": "WhatsApp em 2024?"}) as reply:
        assert reply.status_code == 200
        assert reply.headers["content-type"].startswith("text/event-stream")
        events = sse_events(reply.read().decode("utf-8"))

    names = [name for name, _ in events]
    assert names[0] == "progress" and names[-1] == "answer" and names.count("answer") == 1
    assert [data["kind"] for name, data in events if name == "progress"][:2] == ["context", "planning"]
    assert events[-1][1]["answer"] == "17 clientes."


def test_stream_reports_unexpected_errors(settings: Settings) -> None:
    llm = FakeLLM([])  # an exhausted script raises AssertionError: a bug, not an LLM failure
    client = TestClient(create_app(Assistant(llm, settings)))
    with client.stream("POST", "/ask/stream", json={"question": "?"}) as reply:
        events = sse_events(reply.read().decode("utf-8"))

    assert events[-1][0] == "error" and "AssertionError" not in json.dumps(events[-1][1])


def test_api_key_required_when_configured(settings: Settings) -> None:
    client = TestClient(create_app(p2_assistant(settings), api_key="segredo"))

    assert client.get("/health").status_code == 200  # load balancers probe without the key
    assert client.post("/ask", json={"question": "?"}).status_code == 401
    assert client.post("/ask", json={"question": "?"}, headers={"X-API-Key": "errada"}).status_code == 401
    assert client.post("/ask/stream", json={"question": "?"}).status_code == 401
    ok = client.post("/ask", json={"question": "WhatsApp em 2024?"}, headers={"X-API-Key": "segredo"})
    assert ok.status_code == 200 and ok.json()["answer"] == "17 clientes."


def test_thread_id_keeps_memory_over_api(settings: Settings) -> None:
    llm = FakeLLM([*turn(P4_SQL, "Tickets por canal."), *turn(CHAT_SQL, "No Chat.")])
    client = TestClient(create_app(Assistant(llm, settings)))
    client.post("/ask", json={"question": "Quantos tickets por canal?", "thread_id": "conversa-1"})
    client.post("/ask", json={"question": "E só no Chat?", "thread_id": "conversa-1"})

    assert "# Conversa anterior" in llm.prompts[-1] and "Pergunta: Quantos tickets por canal?" in llm.prompts[-1]


def test_unavailable_model_answers_503(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("LLM_CACHE", "on")
    monkeypatch.setenv("DB_PATH", str(settings.db_path))
    monkeypatch.setenv("TRACE_PATH", str(settings.trace_path))
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setattr("src.api.load_env_file", lambda: None)  # the developer's .env must not leak in
    with TestClient(create_app()) as client:
        reply = client.post("/ask", json={"question": "?"})
        health = client.get("/health")

    assert reply.status_code == 503 and "GOOGLE_API_KEY" in reply.json()["detail"]
    assert health.status_code == 503
