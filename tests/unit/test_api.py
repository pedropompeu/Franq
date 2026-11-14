"""HTTP API over the same Assistant: scripted LLM, real database, no network, no server process."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
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


LOOPBACK = ("127.0.0.1", 50000)
PUBLIC = ("8.8.8.8", 50000)


def local(app: FastAPI) -> TestClient:
    """A client calling from this machine (TestClient's default host, "testclient", is not an address)."""
    return TestClient(app, client=LOOPBACK)


def sse_events(body: str) -> list[tuple[str, dict[str, object]]]:
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def test_health(settings: Settings) -> None:
    assistant = p2_assistant(settings)
    reply = local(create_app(assistant)).get("/health")

    assert reply.status_code == 200
    assert reply.json() == {"status": "ok", "model": "fake-llm", "prompt_version": assistant.prompt_version}


def test_ask_returns_agent_response(settings: Settings) -> None:
    reply = local(create_app(p2_assistant(settings))).post("/ask", json={"question": "WhatsApp em 2024?"})

    assert reply.status_code == 200
    body = reply.json()
    assert body["status"] == "answered" and body["answer"] == "17 clientes."
    assert body["data"]["rows"] == [[17]] and body["trace_id"]


@pytest.mark.parametrize("question", ["", "x" * 501])
def test_ask_validates_question(settings: Settings, question: str) -> None:
    reply = local(create_app(p2_assistant(settings))).post("/ask", json={"question": question})
    assert reply.status_code == 422


def test_ask_validates_thread_id(settings: Settings) -> None:
    client = local(create_app(p2_assistant(settings)))
    reply = client.post("/ask", json={"question": "?", "thread_id": "../etc"})
    assert reply.status_code == 422


def test_stream_emits_progress_then_answer(settings: Settings) -> None:
    client = local(create_app(p2_assistant(settings)))
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
    client = local(create_app(Assistant(llm, settings)))
    with client.stream("POST", "/ask/stream", json={"question": "?"}) as reply:
        events = sse_events(reply.read().decode("utf-8"))

    assert events[-1][0] == "error" and "AssertionError" not in json.dumps(events[-1][1])


def test_api_key_required_when_configured(settings: Settings) -> None:
    client = local(create_app(p2_assistant(settings), api_key="segredo"))

    assert client.get("/health").status_code == 200  # load balancers probe without the key
    assert client.post("/ask", json={"question": "?"}).status_code == 401
    assert client.post("/ask", json={"question": "?"}, headers={"X-API-Key": "errada"}).status_code == 401
    assert client.post("/ask/stream", json={"question": "?"}).status_code == 401
    ok = client.post("/ask", json={"question": "WhatsApp em 2024?"}, headers={"X-API-Key": "segredo"})
    assert ok.status_code == 200 and ok.json()["answer"] == "17 clientes."


def test_thread_id_keeps_memory_over_api(settings: Settings) -> None:
    llm = FakeLLM([*turn(P4_SQL, "Tickets por canal."), *turn(CHAT_SQL, "No Chat.")])
    client = local(create_app(Assistant(llm, settings)))
    opened = client.post("/conversations")
    assert opened.status_code == 201
    thread = opened.json()["thread_id"]
    client.post("/ask", json={"question": "Quantos tickets por canal?", "thread_id": thread})
    client.post("/ask", json={"question": "E só no Chat?", "thread_id": thread})

    assert "# Conversa anterior" in llm.prompts[-1] and "Pergunta: Quantos tickets por canal?" in llm.prompts[-1]


def test_server_issues_unguessable_thread_ids(settings: Settings) -> None:
    client = local(create_app(p2_assistant(settings)))
    ids = {client.post("/conversations").json()["thread_id"] for _ in range(3)}
    assert len(ids) == 3 and all(len(i) >= 22 for i in ids)


@pytest.mark.parametrize("route", ["/ask", "/ask/stream"])
def test_thread_id_not_issued_by_the_server_is_rejected(settings: Settings, route: str) -> None:
    """A chosen or guessed id must not open anyone's history (feedback 2.6)."""
    client = local(create_app(p2_assistant(settings)))
    reply = client.post(route, json={"question": "WhatsApp em 2024?", "thread_id": "conversa-1"})
    assert reply.status_code == 404


def test_thread_id_is_bound_to_the_key_that_opened_it(settings: Settings) -> None:
    llm = FakeLLM([*turn(P4_SQL, "Tickets por canal.")])
    client = TestClient(create_app(Assistant(llm, settings), api_key="chave-a,chave-b"), client=PUBLIC)
    owner, other = {"X-API-Key": "chave-a"}, {"X-API-Key": "chave-b"}
    thread = client.post("/conversations", headers=owner).json()["thread_id"]
    mine = client.post("/ask", json={"question": "Quantos tickets por canal?", "thread_id": thread}, headers=owner)
    assert mine.status_code == 200

    stolen = client.post("/ask", json={"question": "?", "thread_id": thread}, headers=other)
    assert stolen.status_code == 404


def test_without_key_only_the_local_network_is_served(settings: Settings) -> None:
    """No ``API_KEY`` is fine on a laptop or behind compose's 127.0.0.1, not facing the internet (2.5)."""
    exposed = TestClient(create_app(p2_assistant(settings)), client=PUBLIC)
    assert exposed.get("/health").status_code == 200
    assert exposed.post("/ask", json={"question": "?"}).status_code == 403
    assert exposed.post("/conversations").status_code == 403

    docker_bridge = TestClient(create_app(p2_assistant(settings)), client=("172.17.0.1", 50000))
    assert docker_bridge.post("/ask", json={"question": "WhatsApp em 2024?"}).status_code == 200


def test_with_key_any_network_is_served(settings: Settings) -> None:
    client = TestClient(create_app(p2_assistant(settings), api_key="segredo"), client=PUBLIC)
    reply = client.post("/ask", json={"question": "WhatsApp em 2024?"}, headers={"X-API-Key": "segredo"})
    assert reply.status_code == 200


def test_questions_are_rate_limited_per_client(settings: Settings) -> None:
    llm = FakeLLM([*turn(P2_SQL, "17 clientes."), *turn(P2_SQL, "17 clientes.")])
    client = local(create_app(Assistant(llm, settings), rate_per_min=2))
    assert [client.post("/ask", json={"question": "?"}).status_code for _ in range(2)] == [200, 200]

    limited = client.post("/ask/stream", json={"question": "?"})
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    assert client.get("/health").status_code == 200  # probes are never limited
    other = TestClient(client.app, client=("127.0.0.2", 50000))
    assert other.post("/conversations").status_code == 201  # another client has its own window


def test_provider_error_text_never_reaches_the_client(settings: Settings) -> None:
    from src.llm import classify_error

    leak = classify_error(RuntimeError("API key AIzaSyEXEMPLO invalid"), "m")
    client = local(create_app(Assistant(FakeLLM([leak]), settings)))
    reply = client.post("/ask", json={"question": "?"})

    assert reply.status_code == 200 and reply.json()["status"] == "failed"
    assert "AIza" not in reply.text


def test_unavailable_model_answers_503(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("LLM_CACHE", "on")
    monkeypatch.setenv("DB_PATH", str(settings.db_path))
    monkeypatch.setenv("TRACE_PATH", str(settings.trace_path))
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setattr("src.api.load_env_file", lambda: None)  # the developer's .env must not leak in
    with local(create_app()) as client:
        reply = client.post("/ask", json={"question": "?"})
        health = client.get("/health")

    assert reply.status_code == 503 and "GOOGLE_API_KEY" in reply.json()["detail"]
    assert health.status_code == 503
