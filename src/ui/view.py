"""Pure presentation helpers for the Streamlit app: no ``streamlit`` import, fully unit-tested.

Chart rules (BRAND.md + dataviz method): one series in graphite; categorical hues in a fixed
order assigned by entity (sorted), never by rank; at most 8 series, otherwise the table is shown;
thin marks with tooltips; legend whenever there are 2+ series; the table view is always one
click away.
"""

from __future__ import annotations

import csv
import io
import math
import re
import statistics
from collections.abc import Iterable, Sequence
from typing import Any, cast

import altair as alt

from src.agent.models import AgentResponse, ChartSpec
from src.agent.notices import additive_columns, months_between
from src.agent.notices import br_date as br_date  # re-exported for app.py
from src.config import Price
from src.db.tools import QueryResult, SchemaInfo
from src.semantic.dictionary import SemanticDictionary
from src.ui import texts

# Light theme "Grafite e lavanda" (BRAND.md v1.1). Categorical order validated on #E6E9EE and #FFFFFF
# (lightness band, chroma floor, CVD and normal-vision separation). Slots 2-5 sit below 3:1 on the
# page, so the legend and the table view are mandatory, never optional.
CATEGORICAL = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
SINGLE_SERIES = "#2F3A4A"  # one series: graphite, 9.5:1 on the page; the title names it
INK_PRIMARY, INK_MUTED, GRID = "#0E1520", "#465061", "#C9CED6"
MAX_SERIES = len(CATEGORICAL)

# Accents lost in SQL identifiers ("numero_de_clientes" → "Número de clientes").
_ACCENTED = {
    "numero": "número",
    "media": "média",
    "medio": "médio",
    "reclamacoes": "reclamações",
    "reclamacao": "reclamação",
    "nao": "não",
    "mes": "mês",
    "interacoes": "interações",
    "interacao": "interação",
    "ultimo": "último",
    "periodo": "período",
    "avaliacao": "avaliação",
    "satisfacao": "satisfação",
    "regiao": "região",
    "transacoes": "transações",
    "duracao": "duração",
    "preco": "preço",
    "genero": "gênero",
    "resolucao": "resolução",
    "conversao": "conversão",
    "medias": "médias",
    "minimo": "mínimo",
    "maximo": "máximo",
}


def humanize(column: str) -> str:
    """``numero_de_clientes`` → ``Número de clientes``; used for titles, axes and table headers."""
    words = [_ACCENTED.get(w, w) for w in re.split(r"[_\s]+", column.strip().lower()) if w]
    text = " ".join(words)
    return text[:1].upper() + text[1:] if text else column


def is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def records(result: QueryResult) -> list[dict[str, Any]]:
    return [dict(zip(result.columns, row, strict=False)) for row in result.rows]


def column_values(result: QueryResult, column: str) -> list[Any]:
    index = result.columns.index(column)
    return [row[index] for row in result.rows]


def is_numeric_column(result: QueryResult, column: str) -> bool:
    values = [v for v in column_values(result, column) if v is not None]
    return bool(values) and all(is_number(v) for v in values)


def is_integer_column(result: QueryResult, column: str) -> bool:
    """Counts get integer ticks: an axis reading "2,5 clientes" is noise."""
    values = [v for v in column_values(result, column) if v is not None]
    return bool(values) and all(is_number(v) and float(v).is_integer() for v in values)


def _measure_axis(result: QueryResult, column: str) -> alt.Axis:
    if is_integer_column(result, column):
        return alt.Axis(format="d", tickMinStep=1)
    return alt.Axis()


def _tooltip(result: QueryResult, column: str) -> alt.Tooltip:
    title = humanize(column)
    if not is_numeric_column(result, column):
        return alt.Tooltip(f"{column}:N", title=title)
    return alt.Tooltip(f"{column}:Q", title=title, format="d" if is_integer_column(result, column) else ",.2f")


def series_domain(result: QueryResult, column: str) -> list[str]:
    """Sorted so a series keeps its color when filters change the ranking."""
    return sorted({str(v) for v in column_values(result, column)})


def drawable(chart: ChartSpec, result: QueryResult | None) -> ChartSpec:
    """Second validation, on presentation grounds: numeric measure and at most 8 series."""
    if result is None or result.row_count == 0 or chart.type == "table" or chart.y is None:
        return ChartSpec(type="table")
    if chart.y not in result.columns or not is_numeric_column(result, chart.y):
        return ChartSpec(type="table")
    if chart.series is not None and len(series_domain(result, chart.series)) > MAX_SERIES:
        return ChartSpec(type="table")
    return chart


MAX_INFERRED_BARS = 50


def infer_chart(result: QueryResult | None) -> ChartSpec:
    """Bar alternative for a "category, number" result the model chose to show as a table.

    The model's choice stays the default view; this only lets the user switch.
    """
    if result is None or len(result.columns) != 2 or not 2 <= result.row_count <= MAX_INFERRED_BARS:
        return ChartSpec(type="table")
    x, y = result.columns
    if not is_numeric_column(result, x) and is_numeric_column(result, y):
        return ChartSpec(type="bar", x=x, y=y)
    return ChartSpec(type="table")


def format_number(value: Any) -> str:
    """pt-BR formatting: 1234.5 → ``1.234,5``; integers keep no decimals."""
    if not is_number(value):
        return "—" if value is None else str(value)
    text = f"{int(value):,}" if float(value).is_integer() else f"{float(value):,.2f}".rstrip("0").rstrip(".")
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def result_title(chart: ChartSpec, result: QueryResult) -> str:
    """Card title from the chosen encoding: "Número de clientes por estado"."""
    if chart.type == "metric":
        return humanize(chart.y or result.columns[0])
    if chart.x and chart.y:
        title = f"{humanize(chart.y)} por {humanize(chart.x).lower()}"
        return f"{title} e {humanize(chart.series).lower()}" if chart.series else title
    return "Resultado"


def row_caption(result: QueryResult) -> str:
    n = result.row_count
    text = f"{format_number(n)} {'linha' if n == 1 else 'linhas'}"
    return f"{text} (limitado)" if result.truncated else text


def display_records(result: QueryResult) -> list[dict[str, Any]]:
    """Rows with readable headers for the on-screen table; the CSV keeps the SQL names."""
    names = [humanize(c) for c in result.columns]
    if len(set(names)) != len(names):
        names = list(result.columns)
    return [dict(zip(names, row, strict=False)) for row in result.rows]


def metric_value(chart: ChartSpec, result: QueryResult) -> str:
    column = chart.y if chart.y in result.columns else result.columns[0]
    return format_number(result.rows[0][result.columns.index(column)])


def _axis_config(chart: alt.LayerChart | alt.Chart) -> alt.LayerChart | alt.Chart:
    configured = (
        chart.configure(background="transparent")
        .configure_view(stroke=None)
        .configure_axis(
            gridColor=GRID,
            domainColor=GRID,
            tickColor=GRID,
            labelColor=INK_MUTED,
            titleColor=INK_MUTED,
            labelFontSize=12,
            titleFontSize=12,
            titleFontWeight="normal",
            labelLimit=220,
        )
        .configure_legend(labelColor=INK_PRIMARY, titleColor=INK_MUTED, orient="top", symbolType="square")
    )
    return cast(alt.LayerChart | alt.Chart, configured)


_MONTH_LABEL = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


def chart_records(chart: ChartSpec, result: QueryResult) -> tuple[list[dict[str, Any]], list[str] | None]:
    """Rows to draw, and every month of the axis when a line chart's x is ``YYYY-MM`` (feedback 5.2).

    The SQL's GROUP BY has no row for a month without records, and the line would join its
    neighbours over it. For a COUNT or SUM that month is a zero and is drawn as one; any other
    measure (an average) has no value there, so it gets no point, but the axis still shows the month.
    """
    rows = records(result)
    if chart.type != "line" or chart.x not in result.columns or chart.y not in result.columns:
        return rows, None
    xs = column_values(result, chart.x)
    if not xs or not all(isinstance(v, str) and _MONTH_LABEL.fullmatch(v) for v in xs):
        return rows, None
    months = months_between(min(xs), max(xs))
    additive = {result.columns[i] for i in additive_columns(result.sql, len(result.columns))}
    if chart.y in additive:
        groups: list[str | None] = list(series_domain(result, chart.series)) if chart.series else [None]
        present = {(r[chart.x], None if chart.series is None else str(r[chart.series])) for r in rows}
        for month in months:
            for group in groups:
                if (month, group) not in present:
                    filler: dict[str, Any] = {chart.x: month, chart.y: 0}
                    if chart.series is not None:
                        filler[chart.series] = group
                    rows.append(filler)
    return rows, months


def build_chart(chart: ChartSpec, result: QueryResult) -> alt.Chart | alt.LayerChart | None:
    """Altair spec for bar/line; ``None`` for metric and table (rendered by the app)."""
    if chart.type not in ("bar", "line") or chart.x is None or chart.y is None:
        return None
    rows, months = chart_records(chart, result)
    data = alt.InlineData(values=rows)
    tooltip = [_tooltip(result, c) for c in result.columns]
    color: Any = alt.Undefined
    if chart.series is not None:
        domain = series_domain(result, chart.series)
        color = alt.Color(
            f"{chart.series}:N",
            scale=alt.Scale(domain=domain, range=list(CATEGORICAL[: len(domain)])),
            legend=alt.Legend(title=None),
        )

    if chart.type == "bar":
        # Horizontal bars keep long category names readable; sort=None keeps the SQL ORDER BY.
        base = alt.Chart(data).mark_bar(
            cornerRadiusEnd=4, height={"band": 0.7}, color=SINGLE_SERIES if chart.series is None else alt.Undefined
        )
        encoded = base.encode(
            y=alt.Y(f"{chart.x}:N", sort=None, title=None),
            x=alt.X(f"{chart.y}:Q", title=humanize(chart.y), axis=_measure_axis(result, chart.y)),
            tooltip=tooltip,
        )
        if chart.series is not None:
            encoded = encoded.encode(color=color, yOffset=alt.YOffset(f"{chart.series}:N"))
        bars = len({str(v) for v in column_values(result, chart.x)})
        return _axis_config(encoded.properties(height=max(160, 34 * bars)))

    line = (
        alt.Chart(data)
        .mark_line(
            strokeWidth=2,
            color=SINGLE_SERIES if chart.series is None else alt.Undefined,
            point=alt.OverlayMarkDef(size=64, filled=True),
        )
        .encode(
            x=alt.X(
                f"{chart.x}:O",
                sort="ascending",
                title=None,
                axis=alt.Axis(labelAngle=0),
                scale=alt.Scale(domain=months) if months else alt.Undefined,
            ),
            y=alt.Y(f"{chart.y}:Q", title=humanize(chart.y), axis=_measure_axis(result, chart.y)),
            tooltip=tooltip,
        )
    )
    if chart.series is not None:
        line = line.encode(color=color)
    return _axis_config(line.properties(height=320))


def to_csv(result: QueryResult) -> bytes:
    """UTF-8 with BOM so spreadsheet tools open accents correctly."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(result.columns)
    writer.writerows(result.rows)
    return buffer.getvalue().encode("utf-8-sig")


def _words(sql: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql)}


def _columns_named(names: Iterable[str], candidates: Sequence[str]) -> list[str]:
    wanted = {n.lower() for n in names}
    return [c for c in candidates if c.split(".")[-1].lower() in wanted]


def quality_notes(response: AgentResponse, dictionary: SemanticDictionary) -> list[str]:
    """Warnings shown next to the answer. Derived from the dictionary, never hardcoded."""
    notes: list[str] = [n.text for n in response.notices]
    data = response.data
    if data is not None and data.truncated:
        notes.append(texts.QUALITY_TRUNCATED.format(rows=data.row_count))
    if data is not None:
        pii = sorted(
            {c for t in dictionary.tables.values() for c, note in t.columns.items() if note.pii}
            & {c.lower() for c in data.columns}
        )
        if pii:
            notes.append(texts.QUALITY_PII.format(columns=", ".join(pii)))
    if response.corrections:
        notes.append(texts.QUALITY_CORRECTED.format(n=response.corrections))
    final_sql = data.sql if data is not None else ""
    used = _columns_named(_words(final_sql), sorted(dictionary.unreliable_columns()))
    if used:
        notes.append(texts.QUALITY_UNRELIABLE.format(columns=", ".join(used)))
    return notes


def trace_table(traces: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest first, one row per question, for the "Rastros" tab."""
    rows = []
    for t in reversed(traces):
        usage = t.get("usage") or {}
        rows.append(
            {
                "quando (UTC)": str(t.get("timestamp", "")).replace("T", " ").removesuffix("+00:00"),
                "pergunta": t.get("question", ""),
                "status": texts.STATUS_LABEL.get(str(t.get("status")), t.get("status")),
                "modelo": t.get("model", ""),
                "chamadas LLM": t.get("llm_calls", 0),
                "do cache": t.get("cache_hits", 0),
                "correções": t.get("corrections", 0),
                "tokens": int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)),
                "latência (ms)": round(float(t.get("latency_ms", 0.0))),
                "trace_id": t.get("trace_id", ""),
            }
        )
    return rows


def coverage(schema: SchemaInfo, unreliable: frozenset[str]) -> tuple[str, str] | None:
    """First and last date across reliable date columns (dates are ISO strings, so min/max sort)."""
    starts, ends = [], []
    for table in schema.tables:
        for column in table.columns:
            if column.kind != "date" or f"{table.name}.{column.name}" in unreliable:
                continue
            if column.min_value is not None and column.max_value is not None:
                starts.append(str(column.min_value)[:10])
                ends.append(str(column.max_value)[:10])
    return (min(starts), max(ends)) if starts else None


# --- B: trace metrics panel


def _share(part: int, whole: int) -> str:
    return f"{format_number(round(100 * part / whole))}%" if whole else "—"


def _seconds_1(ms: float) -> str:
    return f"{ms / 1000:.1f} s".replace(".", ",")


def _percentile(values: Sequence[float], q: int) -> float:
    """``q``-th percentile with linear interpolation (``statistics.quantiles``, inclusive)."""
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def trace_summary(traces: Sequence[dict[str, Any]], price: Price | None) -> list[tuple[str, str]]:
    """Metric panel of the traces tab: (label, pt-BR value) pairs, in display order.

    Reads only what ``observability.trace_record`` writes; missing fields (older records) count as 0.
    The cost is in US$ per thousand questions and counts every token, including decisions served
    from the cache, so it estimates what the questions cost without the cache.
    """
    if not traces:
        return []
    n = len(traces)
    status = [str(t.get("status", "")) for t in traces]
    calls = sum(int(t.get("llm_calls") or 0) for t in traces)
    hits = sum(int(t.get("cache_hits") or 0) for t in traces)
    latencies = sorted(float(t.get("latency_ms") or 0) for t in traces)
    usage = [t.get("usage") or {} for t in traces]
    tokens_in = sum(int(u.get("input_tokens") or 0) for u in usage)
    tokens_out = sum(int(u.get("output_tokens") or 0) for u in usage)
    rows = [
        (texts.METRIC_QUESTIONS, format_number(n)),
        (texts.METRIC_ANSWERED, _share(status.count("answered"), n)),
        (texts.METRIC_REFUSED, _share(status.count("refused"), n)),
        (texts.METRIC_FAILED, _share(status.count("failed"), n)),
        (texts.METRIC_CORRECTED, _share(sum(1 for t in traces if (t.get("corrections") or 0) > 0), n)),
        (texts.METRIC_CACHED, _share(hits, calls)),
        (texts.METRIC_P50, _seconds_1(_percentile(latencies, 50))),
        (texts.METRIC_P95, _seconds_1(_percentile(latencies, 95))),
        (texts.METRIC_TOKENS, format_number(round((tokens_in + tokens_out) / n))),
    ]
    if price is not None:
        per_thousand = (tokens_in * price.input_per_mtok + tokens_out * price.output_per_mtok) / 1_000 / n
        rows.append((texts.METRIC_COST, f"{per_thousand:.2f}".replace(".", ",")))
    return rows
