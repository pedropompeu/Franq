"""Pseudonymize personal data before it reaches the LLM (and therefore the LLM cache).

The SQL always runs on the real data; the model only ever sees stable tokens. Every row of a
table with ``pii: true`` columns (semantic dictionary) gets a number, and each of its personal
values becomes ``[<column>_<number>]``: ``[nome_7]`` and ``[email_7]`` are the same client.

- Way out (``mask``/``mask_rows``): question, result rows, SQL and errors sent to the model.
- Way back (``unmask_sql``/``unmask``): tokens in the model's SQL become real values before
  execution; tokens in its answer become real values before the user sees them.

Tokens are numbered by ``rowid``, so the same data always yields the same prompt and the
disk cache keeps working. A prompt with no personal data is left byte-for-byte unchanged.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

TOKEN: Final = re.compile(r"\[([a-z_]+)_(\d+)\]")
MIN_SUBSTRING_LEN: Final = 4  # shorter values are only masked when they fill a whole cell

PROMPT_NOTE: Final = (
    "Valores como [nome_3] e [email_3] são pseudônimos de dados pessoais; o mesmo número indica o mesmo "
    "registro. Use-os exatamente como estão, inclusive dentro da SQL (ex.: WHERE nome = '[nome_3]') e na "
    "resposta: o sistema troca pelos valores reais. Nunca tente adivinhar o valor real."
)


def _label(column: str) -> str:
    return re.sub(r"[^a-z_]", "_", column.lower()) or "dado"


@dataclass(frozen=True)
class Pseudonymizer:
    to_token: Mapping[str, str] = field(default_factory=dict)  # casefolded value -> token
    to_value: Mapping[str, str] = field(default_factory=dict)  # token -> value
    _pattern: re.Pattern[str] | None = None

    @classmethod
    def from_rows(cls, rows: Mapping[str, Sequence[str | None]]) -> Pseudonymizer:
        """``{column: values in row order}`` → tokens ``[column_<row number>]`` (1-based)."""
        to_token: dict[str, str] = {}
        to_value: dict[str, str] = {}
        for column, values in rows.items():
            label = _label(column)
            for number, value in enumerate(values, start=1):
                if not value or not value.strip():
                    continue
                token = f"[{label}_{number}]"
                to_token.setdefault(value.casefold(), token)  # duplicates keep the first row
                to_value[token] = value
        long_values = sorted((v for v in to_value.values() if len(v) >= MIN_SUBSTRING_LEN), key=len, reverse=True)
        pattern = None
        if long_values:
            alternation = "|".join(re.escape(v) for v in long_values)
            pattern = re.compile(rf"(?<![\w.@])(?:{alternation})(?![\w@])", re.IGNORECASE)
        return cls(to_token, to_value, pattern)

    @property
    def active(self) -> bool:
        return bool(self.to_value)

    def mask(self, text: str) -> str:
        if self._pattern is None or not text:
            return text
        return self._pattern.sub(lambda m: self.to_token.get(m.group(0).casefold(), m.group(0)), text)

    def mask_cell(self, value: object) -> object:
        if not isinstance(value, str):
            return value
        exact = self.to_token.get(value.casefold())
        return exact if exact is not None else self.mask(value)

    def mask_rows(self, rows: Iterable[Sequence[object]]) -> list[list[object]]:
        return [[self.mask_cell(v) for v in row] for row in rows]

    def unmask(self, text: str) -> str:
        if not self.active or not text:
            return text
        return TOKEN.sub(lambda m: self.to_value.get(m.group(0), m.group(0)), text)

    def unmask_sql(self, sql: str) -> str:
        """Tokens sit inside SQL string literals, so a real value's quotes are doubled."""
        if not self.active or not sql:
            return sql
        return TOKEN.sub(lambda m: self.to_value.get(m.group(0), m.group(0)).replace("'", "''"), sql)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def load_pseudonymizer(db_path: Path, columns: Iterable[str]) -> Pseudonymizer:
    """Read the values of ``table.column`` personal columns (read-only) and build the tokens."""
    by_table: dict[str, list[str]] = defaultdict(list)
    for qualified in sorted(columns):
        table, _, column = qualified.partition(".")
        if table and column:
            by_table[table].append(column)
    rows: dict[str, list[str | None]] = {}
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as con:
        for table, wanted in by_table.items():
            existing = {r[1] for r in con.execute(f"PRAGMA table_info({_quote(table)})")}
            present = [c for c in wanted if c in existing]
            if not present:
                continue
            select = ", ".join(_quote(c) for c in present)
            values = con.execute(f"SELECT {select} FROM {_quote(table)} ORDER BY rowid").fetchall()
            for i, column in enumerate(present):
                label = column if column not in rows else f"{table}_{column}"
                rows[label] = [None if r[i] is None else str(r[i]) for r in values]
    return Pseudonymizer.from_rows(rows)
