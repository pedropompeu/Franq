"""Warnings computed in code from the final SQL and the data, never by the LLM.

* ``tie_at_cut``: a ``LIMIT n`` ranking left out rows tied with the n-th one.
* ``partial_period``: the SQL filters a period that the data only partially cover: a year or month
  (``strftime('%Y', col) = '2024'``, ``col LIKE '2024-07%'``) or a range of days (``BETWEEN``,
  ``>=``/``<``); or a monthly series (``strftime('%Y-%m', col)`` in the result) starts or ends in
  a month the data cover only in part.
* ``empty_period``: the filtered period has no data at all.
* ``trend``: a monthly count series, first half of the period against the second, adjusted by the
  days each half covers, with an exact binomial test and a warning when the monthly volume is low.

Anything this module cannot analyze yields no notice; ``compute_notices`` never raises.
"""

from __future__ import annotations

import logging
import math
import re
from calendar import monthrange
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

from src.agent.models import Notice
from src.db.tools import QueryResult, SchemaInfo, run_sql

if TYPE_CHECKING:
    from pathlib import Path

    from src.config import Settings

logger = logging.getLogger(__name__)

MAX_NAMES = 3
MIN_RERUN_S = 0.05  # below this the tie rerun would only time out
MONTHS = (
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
)  # fmt: skip


def br_date(iso: str) -> str:
    """``2025-11-30`` → ``30/11/2025``; anything else is returned unchanged."""
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso[:10])
    return f"{match[3]}/{match[2]}/{match[1]}" if match else iso


def _parse(sql: str) -> exp.Expr | None:
    try:
        return sqlglot.parse_one(sql, read="sqlite")
    except (sqlglot.errors.SqlglotError, RecursionError):
        return None


# ------------------------------------------------------------------ tie at the cut


def _limit_value(select: exp.Select) -> int | None:
    limit = select.args.get("limit")
    if not isinstance(limit, exp.Limit):
        return None
    value = limit.expression
    if not isinstance(value, exp.Literal) or value.is_string or not value.this.isdigit():
        return None
    return int(value.this)


def _key_index(select: exp.Select, columns: Sequence[str]) -> int | None:
    """Result column of the first ORDER BY key: by alias/name, or by the same expression."""
    order = select.args.get("order")
    if not isinstance(order, exp.Order) or not order.expressions:
        return None
    key = order.expressions[0].this
    if isinstance(key, exp.Literal) and not key.is_string and str(key.this).isdigit():  # ORDER BY 2
        position = int(key.this) - 1
        return position if 0 <= position < len(columns) else None
    if isinstance(key, exp.Column) and not key.table and key.name in columns:
        return list(columns).index(key.name)
    wanted = key.sql(dialect="sqlite")
    for i, projection in enumerate(select.expressions):
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if inner.sql(dialect="sqlite") == wanted:
            return i if i < len(columns) else None
    return None


def _value(v: Any) -> str:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return str(v)
    if isinstance(v, int):
        return f"{v:,}".replace(",", ".")
    return f"{v:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _join(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " e " + names[-1]


def tie_notice(sql: str, result: QueryResult, db_path: Path, *, timeout_s: float) -> Notice | None:
    """Rows tied with the n-th row of a ``LIMIT n`` ranking that the cut left out."""
    tree = _parse(sql)
    if not isinstance(tree, exp.Select) or tree.args.get("offset") is not None:
        return None
    n = _limit_value(tree)
    if n is None or n < 1 or result.row_count != n or result.truncated:
        return None
    k = _key_index(tree, result.columns)
    if k is None:
        return None
    unlimited = tree.copy()
    unlimited.set("limit", None)
    full = run_sql(unlimited.sql(dialect="sqlite"), db_path, max_rows=n + 50, timeout_s=timeout_s)
    if not isinstance(full, QueryResult) or len(full.rows) <= n:
        return None
    cut = full.rows[n - 1][k]
    # Compared with the rows actually shown: with one ORDER BY key the rerun may order ties differently.
    left_out = [row for row in full.rows if row[k] == cut and row not in result.rows]
    if not left_out:
        return None
    at_least = full.truncated and full.rows[-1][k] == cut  # the tie goes on past what was fetched
    label = next((i for i in range(len(full.columns)) if i != k), k)
    names = [str(row[label]) for row in left_out[:MAX_NAMES]]
    if len(left_out) > MAX_NAMES:
        names.append(f"{'pelo menos ' if at_least else ''}mais {len(left_out) - MAX_NAMES}")
    many = len(left_out) > 1 or at_least
    keys = len(tree.args["order"].expressions)
    why = (
        "pelo critério de desempate."
        if keys > 1
        else "; a consulta não define desempate, então quem fica de fora é arbitrário."
    )
    return Notice(
        kind="tie_at_cut",
        text=(
            f"Empate no corte do top {n}: {_join(names)} também {'têm' if many else 'tem'} {_value(cut)} "
            f"e {'ficaram' if many else 'ficou'} de fora{' ' if keys > 1 else ''}{why}"
        ),
    )


# ---------------------------------------------------------------- partial periods

_PERIOD = {"%Y": re.compile(r"\d{4}"), "%Y-%m": re.compile(r"\d{4}-(0[1-9]|1[0-2])")}
_DAY = re.compile(r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])")
_DATE_CALLS = (exp.Date, exp.TsOrDsToDate, exp.DateStrToDate, exp.TsOrDsToTimestamp)  # date(col) keeps the day
_FLIP: dict[type[exp.Expr], type[exp.Expr]] = {exp.GTE: exp.LTE, exp.GT: exp.LT, exp.LTE: exp.GTE, exp.LT: exp.GT}


def _date_column(node: exp.Expr | None) -> exp.Column | None:
    while isinstance(node, _DATE_CALLS):
        node = node.this
    return node if isinstance(node, exp.Column) else None


def _string(node: exp.Expr | None) -> str | None:
    return node.this if isinstance(node, exp.Literal) and node.is_string else None


def _period_filter(node: exp.EQ) -> tuple[exp.Column, str, str] | None:
    """``strftime(fmt, col) = 'literal'`` in either order → (column, fmt, literal)."""
    for call, lit in ((node.this, node.expression), (node.expression, node.this)):
        if not isinstance(call, exp.TimeToStr) or not isinstance(lit, exp.Literal) or not lit.is_string:
            continue
        fmt = call.args.get("format")
        column = call.find(exp.Column)
        if not isinstance(fmt, exp.Literal) or column is None:
            continue
        pattern = _PERIOD.get(fmt.this)
        if pattern is not None and pattern.fullmatch(lit.this):
            return column, fmt.this, lit.this
    return None


def _like_filter(node: exp.Like) -> tuple[exp.Column, str, str] | None:
    """``col LIKE '2024%'`` or ``'2024-07%'`` → (column, fmt, literal); other patterns are not periods."""
    column, pattern = _date_column(node.this), _string(node.expression)
    if column is None or pattern is None or not pattern.endswith("%"):
        return None
    value = pattern[:-1]
    fmt = next((f for f, rx in _PERIOD.items() if rx.fullmatch(value)), None)
    return (column, fmt, value) if fmt else None


def _bounds(fmt: str, value: str) -> tuple[str, str, str]:
    """(first day, last day, label) of a year or a month."""
    if fmt == "%Y":
        return f"{value}-01-01", f"{value}-12-31", f"O ano de {value}"
    year, month = int(value[:4]), int(value[5:7])
    last = monthrange(year, month)[1]
    return f"{value}-01", f"{value}-{last:02d}", f"{MONTHS[month - 1]} de {year}"


def _day_bounds(node: exp.Expr) -> tuple[exp.Column, str | None, str | None] | None:
    """``col BETWEEN a AND b``, ``col >= a``, ``a > col``... over whole days → (column, start, end)."""
    if isinstance(node, exp.Between):
        column, low, high = _date_column(node.this), _string(node.args.get("low")), _string(node.args.get("high"))
        if column is None or low is None or high is None or not (_DAY.match(low) and _DAY.match(high)):
            return None
        return column, low[:10], high[:10]
    kind: type[exp.Expr] = type(node)
    if kind not in _FLIP:
        return None
    column, value = _date_column(node.this), _string(node.expression)
    if column is None:
        column, value, kind = _date_column(node.expression), _string(node.this), _FLIP[kind]
    if column is None or value is None or not _DAY.match(value):
        return None
    day = value[:10]
    if kind is exp.LT and len(value) == 10:  # col < '2025-01-01' ends on 2024-12-31
        day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    return (column, day, None) if kind in (exp.GTE, exp.GT) else (column, None, day)


def _range_label(start: str | None, end: str | None) -> str:
    if start and end:
        return f"O período de {br_date(start)} a {br_date(end)}"
    return f"O período a partir de {br_date(start)}" if start else f"O período até {br_date(end or '')}"


class _Columns:
    """Resolves a column of the query to its table and date-column statistics."""

    def __init__(self, tree: exp.Expr, schema: SchemaInfo) -> None:
        self.aliases = {t.alias_or_name: t.name for t in tree.find_all(exp.Table)}
        self.used = set(self.aliases.values())
        self.schema = schema

    def date_range(self, column: exp.Column) -> tuple[str, str, str] | None:
        """(table, first day, last day) of a date column, when it is unambiguous."""
        tables = [self.aliases[column.table]] if column.table in self.aliases else sorted(self.used)
        candidates = [
            (t.name, c)
            for t in self.schema.tables
            if t.name in tables
            for c in t.columns
            if c.name == column.name and c.kind == "date"
        ]
        if len(candidates) != 1:
            return None
        table, info = candidates[0]
        if info.min_value is None or info.max_value is None:
            return None
        return table, str(info.min_value)[:10], str(info.max_value)[:10]


def _coverage(table: str, lo: str, hi: str, start: str | None, end: str | None, label: str) -> Notice | None:
    if (end is not None and end < lo) or (start is not None and start > hi):
        return Notice(
            kind="empty_period",
            text=f"{label} não tem registros em {table}: os dados vão de {br_date(lo)} a {br_date(hi)}.",
        )
    if (start is not None and start < lo) or (end is not None and end > hi):
        return Notice(
            kind="partial_period",
            text=f"{label} está incompleto nos dados de {table}: os registros vão de {br_date(lo)} a {br_date(hi)}.",
        )
    return None


def _period_map(sql: str, schema: SchemaInfo) -> dict[str, Notice]:
    """Period notices keyed by ``table:period`` (``compras:2025-07``), so a month is warned once."""
    tree = _parse(sql)
    if tree is None:
        return {}
    columns = _Columns(tree, schema)
    found: dict[str, Notice] = {}

    def add(key: str, column: exp.Column, start: str | None, end: str | None, label: str) -> None:
        resolved = columns.date_range(column)
        if resolved is None:
            return
        table, lo, hi = resolved
        notice = _coverage(table, lo, hi, start, end, label)
        if notice is not None:
            found.setdefault(f"{table}:{key}", notice)

    for eq_or_like in tree.find_all(exp.EQ, exp.Like):
        if isinstance(eq_or_like, exp.EQ):
            match = _period_filter(eq_or_like)
        else:
            match = _like_filter(eq_or_like) if isinstance(eq_or_like, exp.Like) else None
        if match is not None:
            column, fmt, value = match
            first, last, label = _bounds(fmt, value)
            add(value, column, first, last, label)

    # Day bounds are merged per SELECT and column: ``>= a AND < b`` is one period, but a bound inside a
    # subquery is a different filter.
    ranges: dict[tuple[int, str, str], tuple[exp.Column, str | None, str | None]] = {}
    for node in tree.find_all(exp.Between, *_FLIP):
        bounds = _day_bounds(node)
        if bounds is None:
            continue
        column, start, end = bounds
        scope = node.find_ancestor(exp.Select)
        key = (id(scope), column.table, column.name)
        _, old_start, old_end = ranges.get(key, (column, None, None))
        ranges[key] = (
            column,
            max(filter(None, (old_start, start)), default=None),
            min(filter(None, (old_end, end)), default=None),
        )
    for column, start, end in ranges.values():
        add(f"{start}..{end}", column, start, end, _range_label(start, end))
    return found


def period_notices(sql: str, schema: SchemaInfo) -> list[Notice]:
    """One notice per filtered period that the column's data do not fully cover, or do not cover at all."""
    return list(_period_map(sql, schema).values())


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def series_notices(sql: str, result: QueryResult, schema: SchemaInfo, *, covered: Iterable[str] = ()) -> list[Notice]:
    """A monthly series whose first or last month the data cover only in part (feedback 5.1).

    ``covered`` holds ``table:YYYY-MM`` keys already warned by ``period_notices``.
    """
    tree = _parse(sql)
    if not isinstance(tree, exp.Select):
        return []
    columns = _Columns(tree, schema)
    skip = set(covered)
    found: list[Notice] = []
    for index, projection in enumerate(tree.selects):
        call = projection.this if isinstance(projection, exp.Alias) else projection
        fmt = call.args.get("format") if isinstance(call, exp.TimeToStr) else None
        column = _date_column(call.this) if isinstance(call, exp.TimeToStr) else None
        if column is None or _string(fmt) != "%Y-%m" or index >= len(result.columns):
            continue
        resolved = columns.date_range(column)
        if resolved is None:
            continue
        table, lo, hi = resolved
        months = {str(row[index]) for row in result.rows}
        ends = []
        if lo[8:10] != "01":
            ends.append(lo[:7])
        if int(hi[8:10]) != monthrange(int(hi[:4]), int(hi[5:7]))[1] and hi[:7] not in ends:
            ends.append(hi[:7])
        partial = [m for m in ends if m in months and f"{table}:{m}" not in skip]
        if not partial:
            continue
        labels = [_bounds("%Y-%m", m)[2] for m in partial]
        many = len(labels) > 1
        found.append(
            Notice(
                kind="partial_period",
                text=(
                    f"{_join([labels[0], *map(_lower_first, labels[1:])])} "
                    f"{'estão incompletos' if many else 'está incompleto'} nos dados de {table}: "
                    f"os registros vão de {br_date(lo)} a {br_date(hi)}. "
                    f"{'As pontas da série contam' if many else 'A ponta da série conta'} menos dias."
                ),
            )
        )
    return found


# ------------------------------------------------------------------ trend of a monthly series

ALPHA = 0.05
MIN_TREND_MONTHS = 6
LOW_VOLUME = 10  # a month with fewer records than this makes a trend claim fragile
MAX_TREND_SERIES = 8
_MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
_SHORT_MONTHS = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")


def _is_flag(node: exp.Expr | None) -> bool:
    """A predicate, or ``CASE WHEN ... THEN 1 ELSE 0 END``: summing it counts rows."""
    if isinstance(node, exp.Predicate):
        return True
    if not isinstance(node, exp.Case):
        return False
    values = [w.args.get("true") for w in node.args.get("ifs", [])] + [node.args.get("default")]
    return all(v is None or (isinstance(v, exp.Literal) and v.this in ("0", "1")) for v in values)


def _measure(projection: exp.Expr) -> str | None:
    """``"count"`` for COUNT(...) or SUM of a 0/1 flag, ``"sum"`` for any other SUM, else ``None``."""
    call = projection.this if isinstance(projection, exp.Alias) else projection
    if isinstance(call, exp.Count):
        return "count"
    if isinstance(call, exp.Sum):
        return "count" if _is_flag(call.this) else "sum"
    return None


def additive_columns(sql: str, width: int) -> frozenset[int]:
    """Result columns that are COUNT or SUM: a month missing from them is a zero, not a gap.

    ``width`` is the number of result columns; a query whose projections do not match it
    (``SELECT *``) yields nothing.
    """
    tree = _parse(sql)
    if not isinstance(tree, exp.Select) or len(tree.selects) != width:
        return frozenset()
    return frozenset(i for i, p in enumerate(tree.selects) if _measure(p) is not None)


def months_between(first: str, last: str) -> list[str]:
    """``'2024-11'``, ``'2025-02'`` → every ``YYYY-MM`` from the first to the last, inclusive."""
    year, month = int(first[:4]), int(first[5:7])
    found: list[str] = []
    while f"{year:04d}-{month:02d}" <= last:
        found.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return found


def short_month(month: str) -> str:
    """``'2024-07'`` → ``'jul/2024'``."""
    return f"{_SHORT_MONTHS[int(month[5:7]) - 1]}/{month[:4]}"


def binomial_two_sided(k: int, n: int, p: float) -> float:
    """Exact two-sided binomial test: probability of an outcome at most as likely as ``k``."""
    if n == 0 or p <= 0 or p >= 1:
        return 1.0

    def log_pmf(i: int) -> float:
        return (
            math.lgamma(n + 1)
            - math.lgamma(i + 1)
            - math.lgamma(n - i + 1)
            + i * math.log(p)
            + (n - i) * math.log1p(-p)
        )

    observed = log_pmf(k)
    return min(1.0, sum(math.exp(q) for q in map(log_pmf, range(n + 1)) if q <= observed + 1e-7))


def _days(month: str, lo: str, hi: str) -> int:
    """Days of ``month`` inside [lo, hi]."""
    first, last, _ = _bounds("%Y-%m", month)
    start, end = max(first, lo), min(last, hi)
    return max(0, (date.fromisoformat(end) - date.fromisoformat(start)).days + 1) if start <= end else 0


def _filter_bounds(tree: exp.Select, column: exp.Column, lo: str, hi: str) -> tuple[str, str]:
    """The data range narrowed by literal day bounds on ``column`` in the outer WHERE."""
    where = tree.args.get("where")
    for node in where.find_all(exp.Between, *_FLIP) if where is not None else ():
        bounds = _day_bounds(node)
        if bounds is None or bounds[0].name != column.name or node.find_ancestor(exp.Select) is not tree:
            continue
        lo, hi = max(lo, bounds[1] or lo), min(hi, bounds[2] or hi)
    return lo, hi


def _pt(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _p_label(p: float) -> str:
    return "p < 0,01" if p < 0.01 else f"p = {_pt(p)}"


def _change(before: int, before_days: int, after: int, after_days: int) -> str:
    if before == 0:
        return "sem registros antes" if after == 0 else "surge na segunda metade"
    change = (after / after_days) / (before / before_days) - 1
    return f"{'+' if change >= 0 else '-'}{abs(change) * 100:.0f}% por dia"


def _series_counts(tree: exp.Select, result: QueryResult, month_at: int) -> dict[str, dict[str, int]] | None:
    """series → month → count, from a long (one count, one label column) or wide (one count per
    series) result; ``None`` when the result has another shape or a non-count measure."""
    kinds = [_measure(p) for p in tree.selects]
    counts = [i for i, k in enumerate(kinds) if k == "count"]
    labels = [i for i, k in enumerate(kinds) if k is None and i != month_at]
    if not counts or "sum" in kinds or len(labels) > 1 or (len(counts) > 1 and labels):
        return None
    series: dict[str, dict[str, int]] = {}
    for row in result.rows:
        month = row[month_at]
        if not isinstance(month, str) or not _MONTH.fullmatch(month):
            return None
        for i in counts:
            value = row[i] if row[i] is not None else 0
            if not isinstance(value, int) or isinstance(value, bool):
                return None
            name = str(row[labels[0]]) if labels else (result.columns[i] if len(counts) > 1 else "Total")
            per_month = series.setdefault(name, {})
            per_month[month] = per_month.get(month, 0) + value
    return series


def trend_notices(sql: str, result: QueryResult, schema: SchemaInfo) -> list[Notice]:
    """Is the monthly series going up or down, or just oscillating? (feedback 5.2)

    The period is split in two halves by days covered; under "no change" each record falls in the
    first half with probability (days of the first half / all days), so an exact binomial test tells
    a trend from noise. The result is a count (COUNT, or SUM of a 0/1 flag), in long format (month,
    label, count) or wide (month, one count per series).
    """
    tree = _parse(sql)
    if not isinstance(tree, exp.Select) or len(tree.selects) != len(result.columns) or result.truncated:
        return []
    columns = _Columns(tree, schema)
    found: list[Notice] = []
    for index, projection in enumerate(tree.selects):
        call = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(call, exp.TimeToStr) or _string(call.args.get("format")) != "%Y-%m":
            continue
        column = _date_column(call.this)
        resolved = columns.date_range(column) if column is not None else None
        series = _series_counts(tree, result, index) if resolved is not None else None
        if column is None or resolved is None or not series or len(series) > MAX_TREND_SERIES:
            continue
        table, lo, hi = resolved
        lo, hi = _filter_bounds(tree, column, lo, hi)
        seen = sorted({m for per_month in series.values() for m in per_month})
        months = [m for m in months_between(seen[0], seen[-1]) if _days(m, lo, hi) > 0]
        if len(months) < MIN_TREND_MONTHS:
            continue
        days = [_days(m, lo, hi) for m in months]
        total, elapsed = sum(days), 0
        first: list[str] = []
        for month, d in zip(months, days, strict=True):
            if elapsed + d / 2 < total / 2:
                first.append(month)
            elapsed += d
        second = months[len(first) :]
        first_days = sum(days[: len(first)])
        share = first_days / total
        parts, significant = [], []
        for name, per_month in sorted(series.items()):
            a = sum(per_month.get(m, 0) for m in first)
            b = sum(per_month.get(m, 0) for m in second)
            p = binomial_two_sided(a, a + b, share)
            if p < ALPHA:
                significant.append(name)
            change = _change(a, first_days, b, total - first_days)
            parts.append(f"{name} {a} → {b} ({change}, {_p_label(p)})")
        monthly = [per_month.get(m, 0) for per_month in series.values() for m in months]
        if not significant:
            verdict = "Nenhuma diferença é estatisticamente significativa (p < 0,05): não há evidência de tendência."
        elif len(significant) == len(series):
            verdict = "A diferença é estatisticamente significativa (p < 0,05)."
        else:
            verb = "têm" if len(significant) > 1 else "tem"
            verdict = f"Só {_join(significant)} {verb} diferença estatisticamente significativa (p < 0,05)."
        volume = ""
        if max(monthly) < LOW_VOLUME:
            volume = (
                f" Com {min(monthly)} a {max(monthly)} registros por mês em {table}, "
                "o volume é baixo para afirmar tendência."
            )
        found.append(
            Notice(
                kind="trend",
                text=(
                    f"Tendência, primeira metade do período ({short_month(first[0])} a {short_month(first[-1])}) "
                    f"contra a segunda ({short_month(second[0])} a {short_month(second[-1])}), ajustada pelos dias "
                    f"cobertos: {'; '.join(parts)}. {verdict}{volume}"
                ),
            )
        )
    return found


def compute_notices(sql: str, result: QueryResult, schema: SchemaInfo, settings: Settings) -> list[Notice]:
    """All notices for the final answer. Never raises: each check that fails just adds nothing."""
    found: list[Notice] = []
    try:
        # One deadline for the answer's SQL and its rerun without LIMIT (feedback 2.7): the rerun gets
        # what the first execution left, and is skipped when that is too little to be worth it.
        left = settings.sql_timeout_s - result.elapsed_ms / 1000
        tie = tie_notice(sql, result, settings.db_path, timeout_s=left) if left >= MIN_RERUN_S else None
        found.extend([tie] if tie else [])
    except Exception as err:
        logger.warning("tie notice skipped: %s", type(err).__name__)
    periods: dict[str, Notice] = {}
    try:
        periods = _period_map(sql, schema)
    except Exception as err:
        logger.warning("period notices skipped: %s", type(err).__name__)
    found.extend(periods.values())
    try:
        found.extend(series_notices(sql, result, schema, covered=periods))
    except Exception as err:
        logger.warning("series notices skipped: %s", type(err).__name__)
    try:
        found.extend(trend_notices(sql, result, schema))
    except Exception as err:
        logger.warning("trend notice skipped: %s", type(err).__name__)
    return found
