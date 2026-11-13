"""Optional LangFuse bridge: it sees only masked messages and never breaks an answer. No test touches the network."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from src.agent.graph import Assistant
from src.agent.models import TokenUsage
from src.config import Settings
from src.evaluation import CaseRun, load_cases, run_evaluation
from src.langfuse_bridge import LangfuseDatasetSink, TracedLLM, langfuse_sink_from_env
from src.llm import StructuredLLM, get_llm
from src.privacy import load_pseudonymizer
from src.run_context import RunInfo
from src.semantic.dictionary import load_dictionary
from tests.conftest import DICTIONARY
from tests.unit.test_agent import P2_SQL, FakeLLM, answer, query
from tests.unit.test_privacy import first_client


class FakeSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


class BrokenSink:
    def record(self, **_kwargs: Any) -> None:
        raise RuntimeError("langfuse fora do ar")


@pytest.fixture
def settings(db_path: Path, tmp_path: Path) -> Settings:
    return dataclasses.replace(Settings.from_env(), db_path=db_path, trace_path=tmp_path / "traces.jsonl")


def test_langfuse_sees_only_masked_messages(settings: Settings) -> None:
    nome, email = first_client(settings.db_path)
    sql = "SELECT nome, email FROM clientes ORDER BY rowid LIMIT 3"
    sink = FakeSink()
    llm = TracedLLM(FakeLLM([query(sql), answer(sql, "O primeiro é [nome_1] ([email_1]).")]), sink)
    response = Assistant(llm, settings).ask(f"Qual o e-mail de {nome}?")
    assert response.status == "answered"
    assert len(sink.records) == 2
    sent = "\n".join(m["content"] for r in sink.records for m in r["input"])
    assert nome not in sent and email not in sent
    assert "[nome_1]" in sent
    outputs = "\n".join(str(r["output"]) for r in sink.records)
    assert nome not in outputs and email not in outputs


def test_sink_failure_does_not_break_answer(settings: Settings) -> None:
    llm = TracedLLM(FakeLLM([query(P2_SQL), answer(P2_SQL, "17.")]), BrokenSink())
    response = Assistant(llm, settings).ask("WhatsApp 2024?")
    assert response.status == "answered"
    assert response.answer == "17."


def test_record_carries_trace_and_prompt_version(settings: Settings) -> None:
    sink = FakeSink()
    assistant = Assistant(TracedLLM(FakeLLM([query(P2_SQL), answer(P2_SQL, "17.")]), sink), settings)
    response = assistant.ask("WhatsApp 2024?")
    first = sink.records[0]
    run: RunInfo = first["run"]
    assert run.trace_id == response.trace_id
    assert run.prompt_version == assistant.prompt_version
    assert first["model"] == "fake-llm"
    assert first["usage"] == TokenUsage(input_tokens=100, output_tokens=10)
    assert first["cached"] is False
    assert first["output"]["action"] == "run_query"
    assert {m["role"] for m in first["input"]} >= {"system", "human"}


def test_traced_llm_keeps_the_inner_model_name() -> None:
    assert TracedLLM(FakeLLM([]), FakeSink()).model_name == "fake-llm"


def test_no_keys_means_no_wrapper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("LLM_CACHE", "replay")
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_MODELS", "gemini-x:15")
    assert langfuse_sink_from_env() is None
    assert not isinstance(get_llm(), TracedLLM)


def test_keys_wrap_the_chain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src import langfuse_bridge

    monkeypatch.setattr(langfuse_bridge, "langfuse_sink_from_env", FakeSink)
    monkeypatch.setenv("LLM_CACHE", "replay")
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_MODELS", "gemini-x:15")
    llm = get_llm()
    assert isinstance(llm, TracedLLM)
    assert llm.model_name == "gemini-x"


# ---------------------------------------------------------------- dataset runs


class FakeItem:
    def __init__(self, *, input: Any, expected_output: Any, **_kwargs: Any) -> None:
        self.input, self.expected_output = input, expected_output


class FakeEvaluation:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class FakeClient:
    """Mimics the langfuse 5.2 calls the dataset sink uses."""

    def __init__(self) -> None:
        self.datasets: list[str] = []
        self.items: list[dict[str, Any]] = []
        self.experiments: list[dict[str, Any]] = []
        self.flushes = 0

    def create_dataset(self, *, name: str, **_kwargs: Any) -> None:
        self.datasets.append(name)

    def create_dataset_item(self, **kwargs: Any) -> FakeItem:
        self.items.append(kwargs)
        return FakeItem(**kwargs)

    def run_experiment(self, *, name: str, run_name: str, data: Sequence[FakeItem], task: Any, evaluators: Any) -> None:
        scores = []
        for item in data:
            output = task(item=item)
            for evaluator in evaluators:
                scores += evaluator(input=item.input, output=output, expected_output=item.expected_output, metadata={})
        self.experiments.append({"name": name, "run_name": run_name, "output": output, "scores": scores})

    def flush(self) -> None:
        self.flushes += 1


def _case_run(case_id: str, *, passed: bool) -> CaseRun:
    case = next(c for c in load_cases() if c.id == case_id)
    return CaseRun(
        case=case,
        status="answered",
        checks={"status": passed},
        llm_calls=2,
        corrections=1,
        cache_hits=0,
        latency_ms=1234.5,
        input_tokens=10,
        output_tokens=2,
        leaks=0,
        sql="SELECT 1",
        model="m",
        trace_id="abc123",
    )


def test_dataset_sink_records_scores() -> None:
    client = FakeClient()
    sink = LangfuseDatasetSink(client, "indaga-eval", evaluation_cls=FakeEvaluation)
    sink.record_case(_case_run("E2", passed=True), run_name="r1")
    sink.record_case(_case_run("P2", passed=False), run_name="r1")
    assert client.datasets == ["indaga-eval"]  # created once
    e2 = next(c for c in load_cases() if c.id == "E2")
    assert client.items[0]["dataset_name"] == "indaga-eval"
    assert client.items[0]["id"] == "indaga-eval-E2"
    assert client.items[0]["input"] == e2.question
    assert client.items[0]["expected_output"] == e2.reference
    first, second = client.experiments
    assert first["name"] == "indaga-eval" and first["run_name"] == "r1"
    assert first["output"]["trace_id"] == "abc123"
    assert {s.name: s.value for s in first["scores"]} == {"acerto": 1, "correcoes": 1, "latencia_ms": 1234.5}
    assert {s.name: s.value for s in second["scores"]}["acerto"] == 0
    assert client.flushes == 2


def test_dataset_items_carry_no_personal_data(db_path: Path) -> None:
    privacy = load_pseudonymizer(db_path, load_dictionary(DICTIONARY).pii_columns())
    client = FakeClient()
    sink = LangfuseDatasetSink(client, "indaga-eval", evaluation_cls=FakeEvaluation)
    for case_id in ("E2", "P2"):
        sink.record_case(_case_run(case_id, passed=True), run_name="r1")
    text = str(client.items) + str([e["output"] for e in client.experiments])
    assert privacy.mask(text) == text


class SpyDatasetSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def record_case(self, run: CaseRun, *, run_name: str) -> None:
        self.calls.append((run.case.id, run_name))


def test_run_evaluation_feeds_the_dataset_sink(settings: Settings) -> None:
    privacy = load_pseudonymizer(settings.db_path, load_dictionary(DICTIONARY).pii_columns())
    cases = [c for c in load_cases() if c.id == "E2"]
    llm = FakeLLM([query(P2_SQL), answer(P2_SQL, "17 clientes.")])
    spy = SpyDatasetSink()

    def factory(guarded: StructuredLLM) -> Assistant:
        return Assistant(guarded, settings, privacy=privacy)

    report = run_evaluation(cases, factory, llm, settings, privacy, dataset_sink=spy)
    [(case_id, run_name)] = spy.calls
    assert case_id == "E2"
    assert run_name.startswith(report.prompt_version)
    assert report.runs[0].trace_id


def test_langfuse_flag_without_keys_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src import evaluation

    monkeypatch.setattr(evaluation, "get_llm", lambda: FakeLLM([]))
    monkeypatch.setattr(evaluation, "run_evaluation", lambda *_a, **_k: pytest.fail("must not run"))
    out = tmp_path / "r.md"
    assert evaluation.main(["--only", "E1", "--no-baseline", "--langfuse", "--output", str(out)]) == 2
    assert not out.exists()
