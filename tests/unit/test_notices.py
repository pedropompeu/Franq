"""Deterministic notices computed in code: ties at the ranking cut and partial periods."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from src.agent import notices
from src.agent.graph import Assistant
from src.agent.notices import compute_notices, period_notices, series_notices, tie_notice
from src.config import Settings
from src.db.tools import QueryResult, SchemaInfo, discover_schema, run_sql
from tests.unit.test_agent import FakeLLM, answer, query

RANKING = (
    "SELECT c.estado, COUNT(DISTINCT p.cliente_id) AS clientes FROM compras p "
    "JOIN clientes c ON c.id = p.cliente_id WHERE p.canal = 'App' "
    "AND strftime('%Y-%m', p.data_compra) = '2025-05' GROUP BY c.estado "
)
E1_SQL = RANKING + "ORDER BY clientes DESC, c.estado LIMIT 5"
E2_SQL = (
    "SELECT COUNT(DISTINCT cliente_id) AS clientes FROM campanhas_marketing "
    "WHERE canal = 'WhatsApp' AND interagiu = 1 AND strftime('%Y', data_envio) = '2024'"
)


@pytest.fixture(scope="module")
def schema(db_path: Path) -> SchemaInfo:
    return discover_schema(db_path)


def executed(sql: str, db_path: Path) -> QueryResult:
    outcome = run_sql(sql, db_path)
    assert isinstance(outcome, QueryResult), outcome
    return outcome


def test_tie_at_cut_names_the_left_out_state(db_path: Path) -> None:
    n = tie_notice(E1_SQL, executed(E1_SQL, db_path), db_path, timeout_s=5)
    assert n is not None and n.kind == "tie_at_cut"
    assert n.text == ("Empate no corte do top 5: Paraná também tem 2 e ficou de fora pelo critério de desempate.")


def test_tie_at_cut_accepts_order_by_expression_in_select(db_path: Path) -> None:
    sql = RANKING.replace("AS clientes", "") + "ORDER BY COUNT(DISTINCT p.cliente_id) DESC, c.estado LIMIT 4"
    n = tie_notice(sql, executed(sql, db_path), db_path, timeout_s=5)
    assert n is not None
    assert n.text == (
        "Empate no corte do top 4: Espírito Santo e Paraná também têm 2 e ficaram de fora pelo critério de desempate."
    )


def test_no_tie_when_cut_is_clean(db_path: Path) -> None:
    sql = RANKING + "ORDER BY clientes DESC LIMIT 1"
    assert tie_notice(sql, executed(sql, db_path), db_path, timeout_s=5) is None


def test_no_tie_without_limit(db_path: Path) -> None:
    sql = RANKING + "ORDER BY clientes DESC"
    assert tie_notice(sql, executed(sql, db_path), db_path, timeout_s=5) is None


@pytest.mark.parametrize(
    "sql",
    [
        RANKING + "ORDER BY clientes DESC LIMIT ?",
        RANKING + "ORDER BY clientes DESC LIMIT 5 OFFSET 2",
        RANKING + "ORDER BY MAX(p.valor) DESC LIMIT 5",
        RANKING + "LIMIT 5",
        "SELEC lixo",
    ],
    ids=["limit-param", "offset", "order-by-hidden-expression", "no-order-by", "unparseable"],
)
def test_tie_notice_skips_unanalyzable_sql(sql: str, db_path: Path) -> None:
    rows = QueryResult(
        sql=sql, columns=["estado", "clientes"], rows=[["X", 1]] * 5, row_count=5, truncated=False, elapsed_ms=1.0
    )
    assert tie_notice(sql, rows, db_path, timeout_s=5) is None


def test_partial_year_2024_for_campaigns(schema: SchemaInfo) -> None:
    [n] = period_notices(E2_SQL, schema)
    assert n.kind == "partial_period"
    assert n.text == (
        "O ano de 2024 está incompleto nos dados de campanhas_marketing: os registros vão de 24/07/2024 a 11/07/2025."
    )


def test_partial_month_july_2025_for_purchases(schema: SchemaInfo) -> None:
    sql = "SELECT COUNT(*) FROM compras p WHERE '2025-07' = strftime('%Y-%m', p.data_compra)"
    [n] = period_notices(sql, schema)
    assert n.text == (
        "Julho de 2025 está incompleto nos dados de compras: os registros vão de 22/07/2024 a 22/07/2025."
    )


def test_full_month_has_no_notice(schema: SchemaInfo) -> None:
    assert period_notices(E1_SQL, schema) == []


def test_period_notices_ignore_unknown_columns(schema: SchemaInfo) -> None:
    assert period_notices("SELECT 1 FROM compras WHERE strftime('%Y', outra) = '2024'", schema) == []


def test_compute_notices_combines_both(db_path: Path, schema: SchemaInfo) -> None:
    sql = E1_SQL.replace("'%Y-%m', p.data_compra) = '2025-05'", "'%Y', p.data_compra) = '2024'")
    settings = dataclasses.replace(Settings.from_env(), db_path=db_path)
    kinds = [n.kind for n in compute_notices(sql, executed(sql, db_path), schema, settings)]
    assert "partial_period" in kinds


@pytest.fixture
def settings(db_path: Path, tmp_path: Path) -> Settings:
    return dataclasses.replace(Settings.from_env(), db_path=db_path, trace_path=tmp_path / "traces.jsonl")


def test_assistant_attaches_notices(settings: Settings) -> None:
    llm = FakeLLM([query(E1_SQL), answer(E1_SQL, "Top 5.")])
    response = Assistant(llm, settings).ask("Top 5 estados no app em maio?")
    assert [n.kind for n in response.notices] == ["tie_at_cut"]


def test_notice_failure_keeps_answer(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object, **_kwargs: object) -> QueryResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(notices, "run_sql", broken)
    llm = FakeLLM([query(E1_SQL), answer(E1_SQL, "Top 5.")])
    response = Assistant(llm, settings).ask("Top 5 estados no app em maio?")
    assert response.status == "answered"
    assert response.notices == []


def test_trace_records_notice_kinds(settings: Settings) -> None:
    from src.observability import Tracer

    tracer = Tracer(settings.trace_path)
    Assistant(FakeLLM([query(E2_SQL), answer(E2_SQL, "17.")]), settings, tracer=tracer).ask("WhatsApp 2024?")
    [record] = tracer.tail(1)
    assert record["notices"] == ["partial_period"]


def test_tie_rerun_uses_only_what_is_left_of_the_sql_deadline(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, schema: SchemaInfo
) -> None:
    """The main SQL and the tie rerun share one ``SQL_TIMEOUT_S`` (feedback 2.7), not one each."""
    budgets: list[float] = []

    def spy(sql: str, path: Path, *, max_rows: int, timeout_s: float) -> object:
        budgets.append(timeout_s)
        return run_sql(sql, path, max_rows=max_rows, timeout_s=timeout_s)

    monkeypatch.setattr(notices, "run_sql", spy)
    settings = dataclasses.replace(Settings.from_env(), db_path=db_path, sql_timeout_s=1.0)
    slow = executed(E1_SQL, db_path).model_copy(update={"elapsed_ms": 700.0})
    compute_notices(E1_SQL, slow, schema, settings)
    assert budgets and budgets[0] == pytest.approx(0.3)

    budgets.clear()
    spent = slow.model_copy(update={"elapsed_ms": 990.0})
    assert not [n for n in compute_notices(E1_SQL, spent, schema, settings) if n.kind == "tie_at_cut"]
    assert budgets == []  # nothing left: the rerun does not even start


# ------------------------------------------------------------------ T7 (feedback 5.1, 5.3, 5.4)


def test_tie_at_cut_resolves_order_by_position(db_path: Path) -> None:
    """``ORDER BY 2`` is common in generated SQL; the notice used to fail silently (feedback 5.3)."""
    sql = RANKING + "ORDER BY 2 DESC, 1 LIMIT 5"
    n = tie_notice(sql, executed(sql, db_path), db_path, timeout_s=5)
    assert n is not None and "Paraná também tem 2" in n.text


def test_tie_at_cut_ignores_position_out_of_range(db_path: Path) -> None:
    sql = RANKING + "ORDER BY 2 DESC, 1 LIMIT 5"
    rows = executed(sql, db_path).model_copy(update={"columns": ["estado"], "rows": [["X"]] * 5})
    assert tie_notice(sql.replace("ORDER BY 2", "ORDER BY 3"), rows, db_path, timeout_s=5) is None


def test_tie_without_tiebreaker_says_the_cut_is_arbitrary(db_path: Path) -> None:
    sql = RANKING + "ORDER BY clientes DESC LIMIT 5"
    n = tie_notice(sql, executed(sql, db_path), db_path, timeout_s=5)
    assert n is not None and "desempate" not in n.text.split(":")[1].split(";")[0]
    assert n.text.endswith("a consulta não define desempate, então quem fica de fora é arbitrário.")


def test_tie_beyond_what_was_fetched_says_at_least(db_path: Path) -> None:
    sql = "SELECT id, 1 AS um FROM compras ORDER BY um DESC, id LIMIT 3"
    n = tie_notice(sql, executed(sql, db_path), db_path, timeout_s=5)
    assert n is not None and "e pelo menos mais 47 também têm 1" in n.text


def test_each_notice_fails_alone(monkeypatch: pytest.MonkeyPatch, db_path: Path, schema: SchemaInfo) -> None:
    sql = E1_SQL.replace("'%Y-%m', p.data_compra) = '2025-05'", "'%Y', p.data_compra) = '2024'")
    settings = dataclasses.replace(Settings.from_env(), db_path=db_path)
    result = executed(sql, db_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    with monkeypatch.context() as m:
        m.setattr(notices, "tie_notice", boom)
        assert [n.kind for n in compute_notices(sql, result, schema, settings)] == ["partial_period"]
    with monkeypatch.context() as m:
        m.setattr(notices, "_period_map", boom)
        assert [n.kind for n in compute_notices(E1_SQL, executed(E1_SQL, db_path), schema, settings)] == ["tie_at_cut"]


@pytest.mark.parametrize(
    ("where", "label"),
    [
        ("p.data_compra LIKE '2024%'", "O ano de 2024"),
        ("p.data_compra LIKE '2025-07%'", "Julho de 2025"),
        ("p.data_compra BETWEEN '2024-01-01' AND '2024-12-31'", "O período de 01/01/2024 a 31/12/2024"),
        ("date(p.data_compra) BETWEEN '2024-01-01' AND '2024-12-31'", "O período de 01/01/2024 a 31/12/2024"),
        ("p.data_compra >= '2024-01-01' AND p.data_compra < '2025-01-01'", "O período de 01/01/2024 a 31/12/2024"),
        ("p.data_compra >= '2024-01-01'", "O período a partir de 01/01/2024"),
        ("'2025-12-31' >= p.data_compra", "O período até 31/12/2025"),
    ],
    ids=["like-year", "like-month", "between", "between-date", "ge-lt", "ge-open", "le-reversed"],
)
def test_partial_period_with_like_between_and_comparisons(schema: SchemaInfo, where: str, label: str) -> None:
    [n] = period_notices(f"SELECT COUNT(*) FROM compras p WHERE {where}", schema)
    assert n.kind == "partial_period"
    assert n.text == f"{label} está incompleto nos dados de compras: os registros vão de 22/07/2024 a 22/07/2025."


@pytest.mark.parametrize(
    "where",
    [
        "p.data_compra BETWEEN '2024-08-01' AND '2024-08-31'",
        "p.data_compra >= '2024-08-01' AND p.data_compra < '2024-09-01'",
        "p.data_compra LIKE '%2024%'",
        "p.data_compra LIKE '2024-08%' OR p.data_compra IS NULL",
    ],
    ids=["covered-between", "covered-ge-lt", "not-a-prefix", "covered-like"],
)
def test_covered_or_unrecognized_ranges_have_no_notice(schema: SchemaInfo, where: str) -> None:
    assert period_notices(f"SELECT COUNT(*) FROM compras p WHERE {where}", schema) == []


def test_bounds_in_a_subquery_are_not_merged_with_the_outer_query(schema: SchemaInfo) -> None:
    sql = (
        "SELECT COUNT(*) FROM compras p WHERE p.data_compra >= '2024-01-01' AND p.cliente_id IN "
        "(SELECT cliente_id FROM compras WHERE data_compra < '2024-09-01')"
    )
    [n] = period_notices(sql, schema)
    assert n.text.startswith("O período a partir de 01/01/2024 está incompleto")  # not "de 01/01 a 31/08"


def test_period_without_any_data_says_so(schema: SchemaInfo) -> None:
    sql = "SELECT COUNT(*) FROM compras p WHERE strftime('%Y-%m', p.data_compra) = '2025-08'"
    [n] = period_notices(sql, schema)
    assert n.kind == "empty_period"
    assert n.text == "Agosto de 2025 não tem registros em compras: os dados vão de 22/07/2024 a 22/07/2025."


TREND_SQL = (
    "SELECT strftime('%Y-%m', data_contato) AS mes, canal, COUNT(*) AS reclamacoes FROM suporte "
    "WHERE tipo_contato = 'Reclamação' GROUP BY mes, canal ORDER BY mes"
)


def test_monthly_series_flags_partial_months_at_the_ends(db_path: Path, schema: SchemaInfo) -> None:
    """Jul/2024 starts on day 23 and jul/2025 ends on day 22: the series' ends count fewer days (5.1)."""
    [n] = series_notices(TREND_SQL, executed(TREND_SQL, db_path), schema)
    assert n.kind == "partial_period"
    assert n.text == (
        "Julho de 2024 e julho de 2025 estão incompletos nos dados de suporte: os registros vão de "
        "23/07/2024 a 22/07/2025. As pontas da série contam menos dias."
    )


def test_monthly_series_inside_the_data_has_no_notice(db_path: Path, schema: SchemaInfo) -> None:
    sql = TREND_SQL.replace("GROUP BY", "AND data_contato BETWEEN '2024-08-01' AND '2025-06-30' GROUP BY")
    assert series_notices(sql, executed(sql, db_path), schema) == []


def test_series_and_period_notices_do_not_repeat_a_month(db_path: Path, schema: SchemaInfo) -> None:
    sql = TREND_SQL.replace("GROUP BY", "AND strftime('%Y-%m', data_contato) = '2025-07' GROUP BY")
    settings = dataclasses.replace(Settings.from_env(), db_path=db_path)
    found = compute_notices(sql, executed(sql, db_path), schema, settings)
    assert sum("Julho de 2025" in n.text or "julho de 2025" in n.text for n in found) == 1
