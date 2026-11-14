"""Warnings computed in code from the final SQL and the data, never by the LLM.

* ``tie_at_cut``: a ``LIMIT n`` ranking left out rows tied with the n-th one.
* ``partial_period``: the SQL filters a period that the data only partially cover: a year or month
  (``strftime('%Y', col) = '2024'``, ``col LIKE '2024-07%'``) or a range of days (``BETWEEN``,
  ``>=``/``<``); or a monthly series (``strftime('%Y-%m', col)`` in the result) starts or ends in
  a month the data cover only in part.
* ``empty_period``: the filtered period has no data at all.

Anything this module cannot analyze yields no notice; ``compute_notices`` never raises.
"""

from __future__ import annotations

import logging
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
    return found
