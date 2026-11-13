"""Presentation rules of the Streamlit app (pure functions, no Streamlit runtime)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agent.models import AgentResponse, ChartSpec, Notice
from src.config import Price, Settings
from src.db.tools import QueryResult, discover_schema
from src.semantic.dictionary import SemanticDictionary, load_dictionary
from src.ui import texts, view


def result(columns: list[str], rows: list[list[Any]], *, truncated: bool = False, sql: str = "SELECT 1") -> QueryResult:
    return QueryResult(
        ok=True, sql=sql, columns=columns, rows=rows, row_count=len(rows), truncated=truncated, elapsed_ms=1.0
    )


@pytest.fixture(scope="module")
def dictionary() -> SemanticDictionary:
    return load_dictionary(Settings.from_env().dictionary_path)


STATES = result(["estado", "clientes"], [["São Paulo", 6], ["Minas Gerais", 3], ["Santa Catarina", 3]])


def test_drawable_keeps_valid_bar() -> None:
    chart = ChartSpec(type="bar", x="estado", y="clientes")
    assert view.drawable(chart, STATES) == chart


def test_drawable_falls_back_when_measure_is_not_numeric() -> None:
    assert view.drawable(ChartSpec(type="bar", x="clientes", y="estado"), STATES).type == "table"


def test_drawable_folds_more_than_eight_series_into_table() -> None:
    rows = [["2025-01", f"canal {i}", i] for i in range(9)]
    data = result(["mes", "canal", "n"], rows)
    assert view.drawable(ChartSpec(type="line", x="mes", y="n", series="canal"), data).type == "table"
    assert view.drawable(
        ChartSpec(type="line", x="mes", y="n", series="canal"), result(data.columns, rows[:8])
    ).type == ("line")


def test_bar_chart_keeps_sql_order_and_single_hue() -> None:
    spec = view.build_chart(ChartSpec(type="bar", x="estado", y="clientes"), STATES)
    assert spec is not None
    d = spec.to_dict()
    assert d["encoding"]["y"]["sort"] is None  # deterministic tie-break from ORDER BY is preserved
    assert d["mark"]["color"] == view.SINGLE_SERIES
    assert d["mark"]["cornerRadiusEnd"] == 4
    values = d["datasets"][d["data"]["name"]] if "name" in d["data"] else d["data"]["values"]
    assert [r["estado"] for r in values] == ["São Paulo", "Minas Gerais", "Santa Catarina"]


def test_series_colors_follow_entity_not_rank() -> None:
    chart = ChartSpec(type="line", x="mes", y="n", series="canal")
    a = result(["mes", "canal", "n"], [["2025-01", "Telefone", 9], ["2025-01", "Chat", 1], ["2025-01", "E-mail", 5]])
    b = result(["mes", "canal", "n"], [["2025-01", "Chat", 9], ["2025-01", "E-mail", 1], ["2025-01", "Telefone", 5]])
    scales = []
    for data in (a, b):
        spec = view.build_chart(chart, data)
        assert spec is not None
        scales.append(spec.to_dict()["encoding"]["color"]["scale"])
    assert scales[0] == scales[1]
    assert scales[0]["domain"] == ["Chat", "E-mail", "Telefone"]
    assert scales[0]["range"] == list(view.CATEGORICAL[:3])


def test_metric_and_table_have_no_altair_spec() -> None:
    assert view.build_chart(ChartSpec(type="metric", y="clientes"), STATES) is None
    assert view.build_chart(ChartSpec(type="table"), STATES) is None


@pytest.mark.parametrize(
    ("value", "text"),
    [(17, "17"), (1234567, "1.234.567"), (1234.5, "1.234,5"), (0.125, "0,12"), (None, "—"), ("SP", "SP")],
)
def test_format_number_pt_br(value: Any, text: str) -> None:
    assert view.format_number(value) == text


def test_metric_value() -> None:
    assert view.metric_value(ChartSpec(type="metric", y="clientes"), result(["clientes"], [[17]])) == "17"


def test_csv_has_bom_header_and_accents() -> None:
    raw = view.to_csv(STATES)
    assert raw.startswith(b"\xef\xbb\xbf")
    lines = raw.decode("utf-8-sig").splitlines()
    assert lines[0] == "estado,clientes" and lines[1] == "São Paulo,6"


def _response(data: QueryResult | None, corrections: int = 0) -> AgentResponse:
    return AgentResponse(question="?", status="answered", answer="ok", data=data, corrections=corrections)


def test_quality_notes(dictionary: SemanticDictionary) -> None:
    assert view.quality_notes(_response(STATES), dictionary) == []
    pii = result(["nome", "email"], [["A", "a@x"]], truncated=True)
    notes = view.quality_notes(_response(pii, corrections=1), dictionary)
    assert any("limitado a 1 linhas" in n for n in notes)
    assert any("dados pessoais (email, nome)" in n for n in notes)
    assert any("corrigiu sozinho (correções: 1)" in n for n in notes)


def test_quality_notes_lists_notices_first(dictionary: SemanticDictionary) -> None:
    response = _response(STATES, corrections=1).model_copy(
        update={"notices": [Notice(kind="tie_at_cut", text="Empate no corte do top 3.")]}
    )
    notes = view.quality_notes(response, dictionary)
    assert notes[0] == "Empate no corte do top 3."
    assert len(notes) == 2


def test_quality_note_flags_unreliable_column(dictionary: SemanticDictionary) -> None:
    data = result(["total"], [[1.0]], sql="SELECT SUM(valor_total_gasto) AS total FROM clientes")
    notes = view.quality_notes(_response(data), dictionary)
    assert any("clientes.valor_total_gasto" in n for n in notes)


def test_coverage_ignores_unreliable_dates(db_path: Path, dictionary: SemanticDictionary) -> None:
    unreliable = dictionary.unreliable_columns()
    schema = discover_schema(db_path, exclude_from_reference=unreliable)
    span = view.coverage(schema, unreliable)
    assert span is not None
    start, end = span
    assert start < end and end == str(schema.reference_date)[:10]


def test_trace_table_is_newest_first() -> None:
    traces = [
        {"timestamp": "2026-11-12T10:00:00+00:00", "question": "a", "status": "answered", "usage": {}},
        {"timestamp": "2026-11-12T11:00:00+00:00", "question": "b", "status": "failed", "usage": {"input_tokens": 3}},
    ]
    rows = view.trace_table(traces)
    assert [r["pergunta"] for r in rows] == ["b", "a"]
    assert rows[0]["status"] == "Sem resposta" and rows[0]["tokens"] == 3
    assert rows[1]["quando (UTC)"] == "2026-11-12 10:00:00"


def test_br_date() -> None:
    assert view.br_date("2025-11-30") == "30/11/2025"
    assert view.br_date("2025-11-30 10:00:00") == "30/11/2025"
    assert view.br_date("n/d") == "n/d"


def test_infer_chart_offers_bars_for_category_and_number() -> None:
    assert view.infer_chart(STATES) == ChartSpec(type="bar", x="estado", y="clientes")
    assert view.infer_chart(result(["clientes"], [[17]])).type == "table"
    assert view.infer_chart(result(["nome", "email"], [["A", "a@x"], ["B", "b@x"]])).type == "table"
    assert view.infer_chart(None).type == "table"


def test_count_axis_has_integer_ticks() -> None:
    spec = view.build_chart(ChartSpec(type="bar", x="estado", y="clientes"), STATES)
    assert spec is not None
    axis = spec.to_dict()["encoding"]["x"]["axis"]
    assert axis == {"format": "d", "tickMinStep": 1}
    avg = result(["categoria", "media"], [["A", 1.5], ["B", 2.0]])
    spec = view.build_chart(ChartSpec(type="bar", x="categoria", y="media"), avg)
    assert spec is not None
    assert spec.to_dict()["encoding"]["x"].get("axis", {}) == {}


def test_tooltip_rounds_averages() -> None:
    avg = result(["categoria", "media"], [["A", 2.21126760563], ["B", 2.0]])
    spec = view.build_chart(ChartSpec(type="bar", x="categoria", y="media"), avg)
    assert spec is not None
    tips = {t["field"]: t for t in spec.to_dict()["encoding"]["tooltip"]}
    assert tips["media"]["format"] == ",.2f" and "format" not in tips["categoria"]


@pytest.mark.parametrize(
    ("column", "label"),
    [
        ("numero_de_clientes", "Número de clientes"),
        ("total_reclamacoes", "Total reclamações"),
        ("mes", "Mês"),
        ("Media_Compras_Por_Cliente", "Média compras por cliente"),
        ("canal", "Canal"),
    ],
)
def test_humanize_restores_spaces_and_accents(column: str, label: str) -> None:
    assert view.humanize(column) == label


def test_result_title_follows_the_encoding() -> None:
    assert view.result_title(ChartSpec(type="bar", x="estado", y="clientes"), STATES) == "Clientes por estado"
    line = ChartSpec(type="line", x="mes", y="total_reclamacoes", series="canal")
    assert view.result_title(line, STATES) == "Total reclamações por mês e canal"
    assert view.result_title(ChartSpec(type="metric", y="total_clientes"), STATES) == "Total clientes"
    assert view.result_title(ChartSpec(type="table"), STATES) == "Resultado"


def test_row_caption() -> None:
    assert view.row_caption(result(["n"], [[1]])) == "1 linha"
    assert view.row_caption(result(["n"], [[1], [2]], truncated=True)) == "2 linhas (limitado)"


def test_display_records_humanizes_headers_but_not_on_collision() -> None:
    assert view.display_records(STATES)[0] == {"Estado": "São Paulo", "Clientes": 6}
    clash = result(["mes", "Mes"], [["2025-01", "jan"]])
    assert view.display_records(clash)[0] == {"mes": "2025-01", "Mes": "jan"}


# --- B: trace metrics panel


def _trace(status: str, latency_ms: float, *, calls: int = 2, hits: int = 0, corrections: int = 0) -> dict[str, Any]:
    return {
        "status": status,
        "latency_ms": latency_ms,
        "llm_calls": calls,
        "cache_hits": hits,
        "corrections": corrections,
        "usage": {"input_tokens": 3000, "output_tokens": 200},
    }


SAMPLE_TRACES = [
    _trace("answered", 1000, hits=2),
    _trace("answered", 2000, corrections=1, calls=3),
    _trace("refused", 3000, calls=1),
    _trace("failed", 10000, calls=2),
]


def test_trace_summary_rates_and_percentiles() -> None:
    rows = dict(view.trace_summary(SAMPLE_TRACES, None))
    assert list(rows) == [
        texts.METRIC_QUESTIONS,
        texts.METRIC_ANSWERED,
        texts.METRIC_REFUSED,
        texts.METRIC_FAILED,
        texts.METRIC_CORRECTED,
        texts.METRIC_CACHED,
        texts.METRIC_P50,
        texts.METRIC_P95,
        texts.METRIC_TOKENS,
    ]
    assert rows[texts.METRIC_QUESTIONS] == "4"
    assert rows[texts.METRIC_ANSWERED] == "50%"
    assert rows[texts.METRIC_REFUSED] == "25%"
    assert rows[texts.METRIC_FAILED] == "25%"
    assert rows[texts.METRIC_CORRECTED] == "25%"
    assert rows[texts.METRIC_CACHED] == "25%"  # 2 of 8 LLM decisions
    assert rows[texts.METRIC_P50] == "2,5 s"
    assert rows[texts.METRIC_P95] == "8,9 s"
    assert rows[texts.METRIC_TOKENS] == "3.200"


def test_trace_summary_empty_is_empty_list() -> None:
    assert view.trace_summary([], None) == []


def test_trace_summary_single_trace_and_old_records() -> None:
    rows = dict(view.trace_summary([{"status": "answered", "latency_ms": 1234}], None))
    assert rows[texts.METRIC_P50] == rows[texts.METRIC_P95] == "1,2 s"
    assert rows[texts.METRIC_CACHED] == "—"  # no LLM calls recorded
    assert rows[texts.METRIC_TOKENS] == "0"


def test_cost_row_only_with_price() -> None:
    assert texts.METRIC_COST not in dict(view.trace_summary(SAMPLE_TRACES, None))
    rows = dict(view.trace_summary(SAMPLE_TRACES, Price(0.30, 2.50)))
    assert list(rows)[-1] == texts.METRIC_COST
    # 3000 * 0,30 + 200 * 2,50 = 1400 US$ per million questions → US$ 1,40 per thousand
    assert rows[texts.METRIC_COST] == "1,40"
