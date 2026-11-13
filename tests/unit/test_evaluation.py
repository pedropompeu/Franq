"""The evaluator itself: result comparison, privacy guard and an end-to-end run with a scripted LLM."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from src import evaluation
from src.agent.graph import Assistant
from src.agent.models import AgentDecision, AgentResponse, Notice
from src.config import Settings
from src.db.tools import QueryResult
from src.evaluation import (
    PLANTED_COLUMN,
    PLANTED_MARKER,
    QUESTIONS_PATH,
    CaseRun,
    EvalCase,
    EvalReport,
    LeakGuard,
    Regression,
    compare_baseline,
    evaluate_response,
    load_cases,
    plant_instruction,
    render_report,
    result_matches,
    run_evaluation,
    run_reference,
    write_baseline,
)
from src.llm import StructuredLLM
from src.privacy import Pseudonymizer, load_pseudonymizer
from src.semantic.dictionary import load_dictionary
from tests.conftest import DICTIONARY
from tests.unit.test_agent import P2_SQL, FakeLLM, answer, query


def case(match: str = "set", **kwargs: Any) -> EvalCase:
    return EvalCase.model_validate(
        {"id": "T1", "kind": "variacao", "question": "?", "expect": ["answered"], "match": match, **kwargs}
    )


REF = (["canal", "n"], [["Chat", 3], ["E-mail", 2], ["Telefone", 2]])


@pytest.fixture
def settings(db_path: Path, tmp_path: Path) -> Settings:
    return dataclasses.replace(Settings.from_env(), db_path=db_path, trace_path=tmp_path / "traces.jsonl")


# ---------------------------------------------------------------- comparison


def test_set_ignores_row_order_column_order_and_extra_columns() -> None:
    agent = (["total", "id", "canal_nome"], [[2, 9, "Telefone"], [3, 7, "Chat"], [2, 8, "E-mail"]])
    assert result_matches(agent, REF, case())


def test_set_rejects_a_wrong_value_or_a_missing_row() -> None:
    assert not result_matches((["canal", "n"], [["Chat", 3], ["E-mail", 2], ["Telefone", 1]]), REF, case())
    assert not result_matches((["canal", "n"], [["Chat", 3], ["E-mail", 2]]), REF, case())


def test_numbers_compare_with_two_decimals() -> None:
    ref = (["media"], [[2.2105263]])
    assert result_matches((["m"], [[2.21]]), ref, case())
    assert result_matches((["n"], [[17.0]]), (["c"], [[17]]), case())
    assert not result_matches((["m"], [[2.2]]), ref, case())


def test_ordered_requires_the_same_order() -> None:
    assert result_matches(REF, REF, case("ordered"))
    assert not result_matches((REF[0], list(reversed(REF[1]))), REF, case("ordered"))


def test_ranking_accepts_any_tie_break_at_the_cut() -> None:
    ranking = (["estado", "n"], [["SP", 6], ["MG", 3], ["AL", 2], ["ES", 2], ["PR", 2], ["BA", 1]])
    top3 = case("ranking", rank_by=1, k=3)
    assert result_matches((["estado", "n"], [["SP", 6], ["MG", 3], ["PR", 2]]), ranking, top3)
    assert result_matches((["estado", "n"], [["MG", 3], ["SP", 6], ["AL", 2]]), ranking, top3)


def test_ranking_rejects_wrong_size_wrong_top_or_unknown_rows() -> None:
    ranking = (["estado", "n"], [["SP", 6], ["MG", 3], ["AL", 2], ["BA", 1]])
    top2 = case("ranking", rank_by=1, k=2)
    assert not result_matches((["estado", "n"], [["SP", 6]]), ranking, top2)
    assert not result_matches((["estado", "n"], [["SP", 6], ["AL", 2]]), ranking, top2)  # skipped MG
    assert not result_matches((["estado", "n"], [["SP", 6], ["RJ", 3]]), ranking, top2)


def test_ranking_without_k_accepts_any_prefix() -> None:
    ranking = (["categoria", "media"], [["Roupas", 2.21], ["Viagens", 2.16], ["Livros", 1.98]])
    any_k = case("ranking", rank_by=1)
    assert result_matches((["categoria", "media"], [["Roupas", 2.21]]), ranking, any_k)
    assert result_matches(ranking, ranking, any_k)
    assert not result_matches((["categoria", "media"], [["Viagens", 2.16]]), ranking, any_k)


# ---------------------------------------------------------------- question file


def test_question_file_is_valid_and_every_reference_runs(db_path: Path) -> None:
    cases = load_cases()
    assert len(cases) >= 15 and len({c.id for c in cases}) == len(cases)
    kinds = {c.kind for c in cases}
    assert {"enunciado", "ambigua", "pii", "autocorrecao", "fora_do_escopo", "escrita", "injecao"} <= kinds
    assert sum(c.kind == "enunciado" for c in cases) == 5
    for c in cases:
        assert set(c.expect) <= {"answered", "refused", "failed"}
        for sql in c.reference:
            columns, rows = run_reference(db_path, sql)
            assert rows, f"{c.id}: reference returned no rows"
            if c.match == "ranking":
                assert 0 <= c.rank_by < len(columns)


def test_question_file_has_no_personal_data(db_path: Path) -> None:
    privacy = load_pseudonymizer(db_path, load_dictionary(DICTIONARY).pii_columns())
    text = QUESTIONS_PATH.read_text(encoding="utf-8")
    assert privacy.mask(text) == text


# ---------------------------------------------------------------- privacy guard


def test_leak_guard_counts_messages_with_real_values() -> None:
    privacy = Pseudonymizer.from_rows({"nome": ["Ana Lima"]})
    guard = LeakGuard(FakeLLM([query("SELECT 1")]), privacy)
    guard.decide([HumanMessage("Quanto [nome_1] gastou?"), HumanMessage("e a Ana Lima?")])
    assert guard.leaks == 1


# ---------------------------------------------------------------- end to end


def test_run_evaluation_scores_answers_and_refusals(settings: Settings) -> None:
    privacy = load_pseudonymizer(settings.db_path, load_dictionary(DICTIONARY).pii_columns())
    cases = [c for c in load_cases() if c.id in ("E2", "P2", "R3")]
    email_sql = "SELECT email FROM clientes WHERE nome = '[nome_1]'"
    llm = FakeLLM(
        [
            query(P2_SQL),
            answer(P2_SQL, "17 clientes."),
            query(email_sql),
            answer(email_sql, "É [email_1]."),
            AgentDecision(thought="injeção", action="refuse", answer="Não posso fazer isso."),
        ]
    )

    def factory(guarded: StructuredLLM) -> Assistant:
        return Assistant(guarded, settings, privacy=privacy)

    report = run_evaluation(cases, factory, llm, settings, privacy)
    assert [r.passed for r in report.runs] == [True, True, True]
    assert report.db_unchanged and sum(r.leaks for r in report.runs) == 0
    assert "[nome_1]" in report.runs[1].sql  # the report keeps the SQL masked
    text = render_report(report)
    assert "| Acerto de execução (resultado = SQL de referência) | 2/2 (100%) |" in text
    assert privacy.mask(text) == text  # no personal data in the report


def test_wrong_result_and_missing_premise_fail(settings: Settings) -> None:
    privacy = Pseudonymizer()
    cases = [c for c in load_cases() if c.id == "A1"]
    wrong = "SELECT COUNT(*) FROM compras"  # total, not an average; no premise either
    llm = FakeLLM([query(wrong), answer(wrong, "946 compras.")])

    def factory(guarded: StructuredLLM) -> Assistant:
        return Assistant(guarded, settings, privacy=privacy)

    run = run_evaluation(cases, factory, llm, settings, privacy).runs[0]
    assert not run.passed
    assert run.failed_checks == ["resultado", "premissa"]


def test_injected_fault_makes_the_model_correct_itself(settings: Settings) -> None:
    privacy = Pseudonymizer()
    cases = [c for c in load_cases() if c.id == "C1"]
    good = "SELECT categoria, SUM(valor) AS total FROM compras GROUP BY categoria"
    # the same SQL twice: the first is corrupted by the evaluator, the second goes through
    llm = FakeLLM([query(good), query(good), answer(good, "Totais por categoria.")])

    def factory(guarded: StructuredLLM) -> Assistant:
        return Assistant(guarded, settings, privacy=privacy)

    run = run_evaluation(cases, factory, llm, settings, privacy).runs[0]
    assert run.passed and run.corrections == 1
    assert "coluna_inexistente" in llm.prompts[1]  # the model is shown the real SQLite error


def test_fault_is_not_injected_when_the_case_does_not_ask(settings: Settings) -> None:
    privacy = Pseudonymizer()
    cases = [c for c in load_cases() if c.id == "E2"]
    llm = FakeLLM([query(P2_SQL), answer(P2_SQL, "17 clientes.")])

    def factory(guarded: StructuredLLM) -> Assistant:
        return Assistant(guarded, settings, privacy=privacy)

    run = run_evaluation(cases, factory, llm, settings, privacy).runs[0]
    assert run.passed and run.corrections == 0


# ---------------------------------------------------------------- instruction planted in the data

CHANNELS_SQL = "SELECT canal, COUNT(*) AS n FROM suporte WHERE tipo_contato = 'Reclamação' GROUP BY canal"


def _planted_run(settings: Settings, final_answer: str) -> tuple[Any, FakeLLM]:
    privacy = Pseudonymizer()
    cases = [c for c in load_cases() if c.id == "D1"]
    llm = FakeLLM([query(CHANNELS_SQL), answer(CHANNELS_SQL, final_answer)])

    def factory(guarded: StructuredLLM) -> Assistant:
        return Assistant(guarded, settings, privacy=privacy)

    return run_evaluation(cases, factory, llm, settings, privacy).runs[0], llm


def test_planted_instruction_reaches_the_model_inside_the_data_block(settings: Settings) -> None:
    run, llm = _planted_run(settings, "Chat lidera as reclamações.")
    assert run.passed and run.checks["resisted"]
    block = llm.prompts[1].split("<dados>\n")[1]
    assert PLANTED_MARKER in block and PLANTED_COLUMN in block
    assert PLANTED_MARKER not in run.sql  # the query itself is untouched


def test_obeying_the_planted_instruction_fails_the_case(settings: Settings) -> None:
    run, _ = _planted_run(settings, PLANTED_MARKER)
    assert not run.passed and run.failed_checks == ["obedeceu à instrução nos dados"]


def test_plant_instruction_leaves_text_without_data_unchanged() -> None:
    assert plant_instruction("sem bloco de dados") == "sem bloco de dados"


# ---------------------------------------------------------------- notices


def _answered(rows: int, notices: list[Notice]) -> AgentResponse:
    data = QueryResult(
        sql="SELECT 1", columns=["estado", "n"], rows=[["X", 1]] * rows, row_count=rows, truncated=False, elapsed_ms=1
    )
    return AgentResponse(question="?", status="answered", answer="ok", data=data, notices=notices)


def _notice_check(c: EvalCase, response: AgentResponse) -> bool:
    return evaluate_response(c, response, [], Pseudonymizer(), 0)["notice"]


def test_tie_check_accepts_notice_or_all_tied_rows() -> None:
    e1 = case("ranking", rank_by=1, k=5, expect_notice="tie_at_cut")
    tie = Notice(kind="tie_at_cut", text="Empate no corte do top 5.")
    assert _notice_check(e1, _answered(5, [tie]))
    assert _notice_check(e1, _answered(6, []))
    assert not _notice_check(e1, _answered(5, []))
    assert evaluation.CHECK_LABEL["notice"] == "aviso"


def test_partial_period_check_requires_notice() -> None:
    e2 = case(expect_notice="partial_period")
    partial = Notice(kind="partial_period", text="O ano de 2024 está incompleto.")
    assert _notice_check(e2, _answered(1, [partial]))
    assert not _notice_check(e2, _answered(1, []))
    assert not _notice_check(e2, _answered(1, [Notice(kind="tie_at_cut", text="x")]))
    assert "notice" not in evaluate_response(case(), _answered(1, []), [], Pseudonymizer(), 0)


# ---------------------------------------------------------------- baseline


def _run(case_id: str, passed: bool) -> CaseRun:
    return CaseRun(
        case=case(id=case_id),
        status="answered",
        checks={"status": passed},
        llm_calls=1,
        corrections=0,
        cache_hits=0,
        latency_ms=1.0,
        input_tokens=1,
        output_tokens=1,
        leaks=0,
        sql="",
        model="m",
    )


def _report(*runs: tuple[str, bool], repeat: int = 1) -> EvalReport:
    return EvalReport(
        runs=[_run(i, ok) for i, ok in runs],
        repeat=repeat,
        cache_mode="off",
        models="m",
        db_unchanged=True,
        started=datetime(2026, 11, 13, 20, 0),
        elapsed_s=1.0,
        prompt_version="abcd1234",
    )


def test_baseline_round_trip(tmp_path: Path) -> None:
    report = _report(("E1", True), ("E2", False), ("E1", True), ("E2", True), repeat=2)
    path = tmp_path / "avaliacao.json"
    write_baseline(report, path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["prompt_version"] == "abcd1234" and stored["models"] == "m"
    assert stored["cases"] == {"E1": {"passed": 2, "runs": 2}, "E2": {"passed": 1, "runs": 2}}
    assert compare_baseline(report, stored) == []


def test_regression_detected_when_pass_rate_drops(tmp_path: Path) -> None:
    path = tmp_path / "avaliacao.json"
    write_baseline(_report(("E1", True), ("E1", True), ("E1", True), repeat=3), path)
    after = _report(("E1", True), ("E1", False), ("E1", True), repeat=3)
    assert compare_baseline(after, json.loads(path.read_text(encoding="utf-8"))) == [Regression("E1", 1.0, 0.667)]


def test_baseline_compares_common_cases_only() -> None:
    baseline = {"cases": {"X9": {"passed": 3, "runs": 3}}}
    assert compare_baseline(_report(("N1", False)), baseline) == []


def test_report_shows_prompt_version_and_comparison() -> None:
    report = _report(("E1", False))
    text = render_report(report, [Regression("E1", 1.0, 0.0)])
    assert "prompt `abcd1234`" in text
    assert "## Comparação com a rodada anterior" in text and "| E1 | 100% | 0% |" in text
    assert "## Comparação com a rodada anterior" not in render_report(report)


def test_main_exits_3_on_regression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = tmp_path / "avaliacao.json"
    write_baseline(_report(("E1", True)), baseline)
    monkeypatch.setenv("LLM_CACHE", "off")
    monkeypatch.setattr(evaluation, "get_llm", lambda: FakeLLM([]))
    monkeypatch.setattr(evaluation, "run_evaluation", lambda *_a, **_k: _report(("E1", False)))
    out = tmp_path / "r.md"
    assert evaluation.main(["--only", "E1", "--baseline", str(baseline), "--output", str(out)]) == 3
    assert "| E1 | 100% | 0% |" in out.read_text(encoding="utf-8")
    assert evaluation.main(["--only", "E1", "--no-baseline", "--output", str(out)]) == 0


def test_main_saves_baseline_only_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = tmp_path / "avaliacao.json"
    monkeypatch.setenv("LLM_CACHE", "off")
    monkeypatch.setattr(evaluation, "get_llm", lambda: FakeLLM([]))
    monkeypatch.setattr(evaluation, "run_evaluation", lambda *_a, **_k: _report(("E1", True)))
    args = ["--baseline", str(baseline), "--output", str(tmp_path / "r.md")]
    assert evaluation.main(args) == 0 and not baseline.exists()
    assert evaluation.main([*args, "--save-baseline"]) == 0
    assert json.loads(baseline.read_text(encoding="utf-8"))["cases"] == {"E1": {"passed": 1, "runs": 1}}


# ---------------------------------------------------------------- follow-up questions


class ThreadSpyAssistant:
    """Records (question, thread_id) and answers every question the same way."""

    prompt_version = "spy"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def ask(self, question: str, *, thread_id: str | None = None, on_progress: object = None) -> AgentResponse:
        self.calls.append((question, thread_id))
        return AgentResponse(question=question, status="answered", answer="18.")


def test_context_questions_share_a_thread(settings: Settings) -> None:
    privacy = load_pseudonymizer(settings.db_path, load_dictionary(DICTIONARY).pii_columns())
    followup = case(id="M9", kind="acompanhamento", context=["Reclamações por canal?"], question="E só no Chat?")
    spy = ThreadSpyAssistant()
    guard = LeakGuard(FakeLLM([]), privacy)
    injector = evaluation.FaultInjector(guard)
    for _ in range(2):
        evaluation.run_case(followup, spy, guard, injector, [], privacy)  # type: ignore[arg-type]
    (q1, t1), (q2, t2), (q3, t3), (q4, t4) = spy.calls
    assert (q1, q2, q3, q4) == ("Reclamações por canal?", "E só no Chat?", "Reclamações por canal?", "E só no Chat?")
    assert t1 is not None and t1 == t2
    assert t3 is not None and t3 == t4
    assert t1 != t3  # each execution starts a fresh conversation


def test_question_without_context_has_no_thread(settings: Settings) -> None:
    privacy = load_pseudonymizer(settings.db_path, load_dictionary(DICTIONARY).pii_columns())
    spy = ThreadSpyAssistant()
    guard = LeakGuard(FakeLLM([]), privacy)
    evaluation.run_case(case(), spy, guard, evaluation.FaultInjector(guard), [], privacy)  # type: ignore[arg-type]
    assert spy.calls == [("?", None)]


def test_followup_cases_are_labelled() -> None:
    followups = [c for c in load_cases() if c.kind == "acompanhamento"]
    assert {c.id for c in followups} == {"M1", "M2"}
    assert all(c.context for c in followups)
    assert evaluation.KIND_LABEL["acompanhamento"] == "Acompanhamento"
