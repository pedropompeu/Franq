"""Warnings computed in code from the final SQL and the data, never by the LLM.

* ``tie_at_cut``: a ``LIMIT n`` ranking left out rows tied with the n-th one.
* ``partial_period``: the SQL filters a whole year or month (``strftime('%Y', col) = '2024'``)
  that the data only partially cover.

Anything this module cannot analyze yields no notice; ``compute_notices`` never raises.
"""

from __future__ import annotations

import logging
import re
from calendar import monthrange
from collections.abc import Sequence
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
    left_out = [row for row in full.rows[n:] if row[k] == cut]
    if not left_out:
        return None
    label = next((i for i in range(len(full.columns)) if i != k), k)
    names = [str(row[label]) for row in left_out[:MAX_NAMES]]
    if len(left_out) > MAX_NAMES:
        names.append(f"mais {len(left_out) - MAX_NAMES}")
    many = len(left_out) > 1
    return Notice(
        kind="tie_at_cut",
        text=(
            f"Empate no corte do top {n}: {_join(names)} também {'têm' if many else 'tem'} {_value(cut)} "
            f"e {'ficaram' if many else 'ficou'} de fora pelo critério de desempate."
        ),
    )


# ---------------------------------------------------------------- partial periods

_PERIOD = {"%Y": re.compile(r"\d{4}"), "%Y-%m": re.compile(r"\d{4}-(0[1-9]|1[0-2])")}


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


def _bounds(fmt: str, value: str) -> tuple[str, str, str]:
    """(first day, last day, label) of a year or a month."""
    if fmt == "%Y":
        return f"{value}-01-01", f"{value}-12-31", f"O ano de {value}"
    year, month = int(value[:4]), int(value[5:7])
    last = monthrange(year, month)[1]
    return f"{value}-01", f"{value}-{last:02d}", f"{MONTHS[month - 1]} de {year}"


def period_notices(sql: str, schema: SchemaInfo) -> list[Notice]:
    """One notice per filtered year/month that the column's data do not fully cover."""
    tree = _parse(sql)
    if tree is None:
        return []
    aliases = {t.alias_or_name: t.name for t in tree.find_all(exp.Table)}
    used = set(aliases.values())
    found: dict[str, Notice] = {}
    for node in tree.find_all(exp.EQ):
        match = _period_filter(node)
        if match is None:
            continue
        column, fmt, value = match
        tables = [aliases[column.table]] if column.table in aliases else sorted(used)
        candidates = [
            (t.name, c)
            for t in schema.tables
            if t.name in tables
            for c in t.columns
            if c.name == column.name and c.kind == "date"
        ]
        if len(candidates) != 1:
            continue
        table, info = candidates[0]
        if info.min_value is None or info.max_value is None:
            continue
        lo, hi = str(info.min_value)[:10], str(info.max_value)[:10]
        start, end, label = _bounds(fmt, value)
        if lo <= start and end <= hi:
            continue
        found[f"{table}:{value}"] = Notice(
            kind="partial_period",
            text=(f"{label} está incompleto nos dados de {table}: os registros vão de {br_date(lo)} a {br_date(hi)}."),
        )
    return list(found.values())


def compute_notices(sql: str, result: QueryResult, schema: SchemaInfo, settings: Settings) -> list[Notice]:
    """All notices for the final answer. Never raises: a failure means no notice."""
    try:
        tie = tie_notice(sql, result, settings.db_path, timeout_s=settings.sql_timeout_s)
        return ([tie] if tie else []) + period_notices(sql, schema)
    except Exception as err:
        logger.warning("notices skipped: %s", type(err).__name__)
        return []
