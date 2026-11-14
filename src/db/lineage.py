"""Which result columns carry personal data, even transformed (feedback 2026-11-14, 2.3).

``substr(nome, 1, 3)`` or ``replace(email, '@', ' ')`` return pieces of a personal value that the
whole-value pseudonymizer cannot recognize. sqlglot's column lineage follows each result column
back to its source columns through CTEs, subqueries, unions and aliases; a column that reaches a
``pii`` column is masked before the LLM sees it (``Pseudonymizer.mask_rows(derived=...)``).

When the analysis fails the answer is conservative: every column is treated as derived as long
as the SQL mentions a personal column at all.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage
from sqlglot.optimizer.qualify import qualify

Schema = Mapping[str, Mapping[str, str]]  # table -> column -> declared type


def _mentions_pii(sql: str, pii: frozenset[str]) -> bool:
    names = {qualified.partition(".")[2].lower() for qualified in pii}
    words = {w.lower() for w in re.findall(r"\w+", sql)}
    return bool(names & words)


def _sources(column: str, tree: exp.Query, schema: Schema) -> set[str]:
    result = lineage(column, tree, schema=dict(schema), dialect="sqlite")
    nodes = result.values() if isinstance(result, dict) else [result]
    found: set[str] = set()
    for n in (n for root in nodes for n in root.walk()):
        if not n.downstream and isinstance(n.source, exp.Table):
            found.add(f"{n.source.name}.{n.name.split('.')[-1].strip(chr(34))}".lower())
    return found


def pii_derived_columns(sql: str, schema: Schema, pii: frozenset[str], *, width: int | None = None) -> frozenset[int]:
    """Indexes of the result columns of ``sql`` that come from a ``table.column`` in ``pii``.

    ``width`` is the number of result columns, used only by the conservative fallback.
    """
    if not pii:
        return frozenset()
    wanted = {p.lower() for p in pii}
    mentions = _mentions_pii(sql, pii)
    try:
        tree = qualify(
            sqlglot.parse_one(sql, read="sqlite"),
            schema=dict(schema),
            dialect="sqlite",
            validate_qualify_columns=False,
        )
        if not isinstance(tree, exp.Query):
            return frozenset(range(width or 0)) if mentions else frozenset()
        names = [projection.alias_or_name for projection in tree.selects]
        if width is not None and len(names) != width:  # the analysis does not match what ran
            return frozenset(range(width)) if mentions else frozenset()
        derived: set[int] = set()
        for index, name in enumerate(names):
            if names.count(name) > 1:  # lineage looks columns up by name: ambiguous, assume the worst
                hit = mentions
            else:
                try:
                    hit = bool(_sources(name, tree, schema) & wanted)
                except Exception:  # a column sqlglot cannot follow: assume the worst for it
                    hit = mentions
            if hit:
                derived.add(index)
        return frozenset(derived)
    except (sqlglot.errors.SqlglotError, RecursionError, ValueError, KeyError, AttributeError):
        return frozenset(range(width or 0)) if mentions else frozenset()
