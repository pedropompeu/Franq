"""Pseudonymize personal data before it reaches the LLM (and therefore the LLM cache).

The SQL always runs on the real data; the model only ever sees stable tokens. Every row of a
table with ``pii: true`` columns (semantic dictionary) gets a number, and each of its personal
values becomes ``[<column>_<number>]``: ``[nome_7]`` and ``[email_7]`` are the same client.

- Way out (``mask``/``mask_rows``): question, result rows, SQL and errors sent to the model.
- Way back (``unmask_sql``/``unmask``): tokens in the model's SQL become real values before
  execution; tokens in its answer become real values before the user sees them.

Tokens are numbered by ``rowid``, so the same data always yields the same prompt and the
disk cache keeps working. A prompt with no personal data is left byte-for-byte unchanged.

Matching ignores case and accents ("Aragao" finds "Aragão"). Pieces of a personal value that a
query derives (``substr(nome, 1, 5)``, the part of an e-mail before ``@``) are not whole values,
so the columns that carry them (``src/db/lineage.py``) become ``[derivado_<n>]``: an HMAC whose
key comes from the data itself, stable for the cache and not reversible by a dictionary of names.
They go back to real values only from the results of the same turn (``derived_map``).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from src.db.tools import readonly_uri

TOKEN: Final = re.compile(r"\[([a-z_]+)_(\d+)\]")
MIN_SUBSTRING_LEN: Final = 4  # shorter values are only masked when they fill a whole cell
DERIVED_LABEL: Final = "derivado"
_ALNUM = re.compile(r"[^\W_]")

PROMPT_NOTE: Final = (
    "Valores como [nome_3] e [email_3] são pseudônimos de dados pessoais; o mesmo número indica o mesmo "
    "registro. Use-os exatamente como estão, inclusive dentro da SQL (ex.: WHERE nome = '[nome_3]') e na "
    "resposta: o sistema troca pelos valores reais. Nunca tente adivinhar o valor real. [derivado_N] é um "
    "trecho de dado pessoal (ex.: primeiro nome) e segue a mesma regra."
)


def _label(column: str) -> str:
    return re.sub(r"[^a-z_]", "_", column.lower()) or "dado"


def _base(char: str) -> str:
    decomposed = unicodedata.normalize("NFD", char)
    if len(decomposed) > 1 and all(unicodedata.combining(c) for c in decomposed[1:]):
        return decomposed[0]
    return char


def fold(text: str) -> str:
    """``text`` without accents, one character for each one: positions still point at ``text``."""
    return "".join(_base(c) for c in text)


def _key(value: str) -> str:
    return fold(unicodedata.normalize("NFC", value)).casefold()


@dataclass(frozen=True)
class Pseudonymizer:
    to_token: Mapping[str, str] = field(default_factory=dict)  # casefolded value -> token
    to_value: Mapping[str, str] = field(default_factory=dict)  # token -> value
    _pattern: re.Pattern[str] | None = None  # matches folded text
    _secret: bytes = b""  # HMAC key of derived tokens, taken from the personal values

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
                to_token.setdefault(_key(value), token)  # duplicates keep the first row
                to_value[token] = value
        long_values = sorted(
            {fold(unicodedata.normalize("NFC", v)) for v in to_value.values() if len(v) >= MIN_SUBSTRING_LEN},
            key=lambda v: (-len(v), v),
        )
        pattern = None
        if long_values:
            alternation = "|".join(re.escape(v) for v in long_values)
            pattern = re.compile(rf"(?<![\w.@])(?:{alternation})(?![\w@])", re.IGNORECASE)
        secret = hashlib.sha256("\n".join(f"{t}={v}" for t, v in sorted(to_value.items())).encode()).digest()
        return cls(to_token, to_value, pattern, secret)

    @property
    def active(self) -> bool:
        return bool(self.to_value)

    def mask(self, text: str) -> str:
        if self._pattern is None or not text:
            return text
        source = unicodedata.normalize("NFC", text)
        parts: list[str] = []
        last = 0
        for m in self._pattern.finditer(fold(source)):
            token = self.to_token.get(_key(m.group(0)))
            if token is not None:
                parts += [source[last : m.start()], token]
                last = m.end()
        if not parts:
            return text  # nothing personal: byte for byte the same, so the cache stays valid
        return "".join([*parts, source[last:]])

    def mask_cell(self, value: object) -> object:
        if not isinstance(value, str):
            return value
        exact = self.to_token.get(_key(value))
        return exact if exact is not None else self.mask(value)

    def derived_token(self, value: str) -> str:
        digest = hmac.new(self._secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"[{DERIVED_LABEL}_{int(digest[:12], 16) % 10**9}]"

    def _derived_cell(self, value: object) -> object:
        masked = self.mask_cell(value)
        if not isinstance(value, str) or not isinstance(masked, str):
            return masked
        if not _ALNUM.search(TOKEN.sub("", masked)):
            return masked  # only whole tokens and separators, e.g. group_concat(nome)
        return self.derived_token(value)

    def mask_rows(self, rows: Iterable[Sequence[object]], *, derived: Collection[int] = ()) -> list[list[object]]:
        """``derived``: indexes of columns computed from personal data (``src/db/lineage.py``)."""
        return [
            [self._derived_cell(v) if i in derived else self.mask_cell(v) for i, v in enumerate(row)] for row in rows
        ]

    def derived_map(self, rows: Iterable[Sequence[object]], derived: Collection[int]) -> dict[str, str]:
        """``[derivado_<n>]`` -> real value for the derived cells of ``rows``."""
        found: dict[str, str] = {}
        for row in rows:
            for i in derived:
                value = row[i] if i < len(row) else None
                if isinstance(value, str):
                    masked = self._derived_cell(value)
                    if isinstance(masked, str) and masked.startswith(f"[{DERIVED_LABEL}_"):
                        found[masked] = value
        return found

    def unmask(
        self, text: str, *, allowed: Collection[str] | None = None, extra: Mapping[str, str] | None = None
    ) -> str:
        """Tokens back to real values; with ``allowed``, only those (the rest stay as tokens).

        ``extra`` holds the derived tokens of the turn (``derived_map``).
        """
        if not (self.active or extra) or not text:
            return text
        extra = extra or {}

        def real(m: re.Match[str]) -> str:
            token = m.group(0)
            if allowed is not None and token not in allowed:
                return token
            return self.to_value.get(token) or extra.get(token, token)

        return TOKEN.sub(real, text)

    def tokens_in(self, text: str) -> set[str]:
        return {m.group(0) for m in TOKEN.finditer(text)} if text else set()

    def unmask_sql(self, sql: str, *, extra: Mapping[str, str] | None = None) -> str:
        """Tokens sit inside SQL string literals, so a real value's quotes are doubled."""
        if not (self.active or extra) or not sql:
            return sql
        extra = extra or {}

        def real(m: re.Match[str]) -> str:
            token = m.group(0)
            return (self.to_value.get(token) or extra.get(token, token)).replace("'", "''")

        return TOKEN.sub(real, sql)


# Markdown or HTML that makes the reader's browser fetch a URL: a model steered by injected text could
# smuggle real values out in it (feedback 2026-11-14, 2.1). Data answers never need any of it.
_IMAGE = re.compile(r"!\[[^\]]*\](?:\([^)]*\)|\[[^\]]*\])")
_LINK_DEFINITION = re.compile(r"^[ \t]*\[[^\]]+\]:[ \t]*\S+.*$", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<\s*/?\s*(?:img|a|iframe|link|script|style|video|audio|source|object|embed)\b[^>]*>", re.I)
_URL = re.compile(r"<?\b(?:https?|ftp|data|javascript):[^\s<>)\]]*>?", re.IGNORECASE)
LINK_REMOVED: Final = "[link removido]"


def strip_external(text: str) -> str:
    """Drop images, links, URLs and active HTML from model text before anyone renders it."""
    if not text:
        return text
    out = _HTML_TAG.sub("", text)
    out = _IMAGE.sub("", out)
    out = _LINK_DEFINITION.sub("", out)
    out = _LINK.sub(r"\1", out)
    out = _URL.sub(LINK_REMOVED, out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


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
    with closing(sqlite3.connect(readonly_uri(db_path), uri=True)) as con:
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
