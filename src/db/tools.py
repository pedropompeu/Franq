"""Read-only SQL execution with layered guardrails, plus dynamic schema discovery.

Security layers, in order (any one of them is enough to stop a write):
1. AST validation with sqlglot: exactly one statement whose root is a SELECT or a set
   operation (UNION/INTERSECT/EXCEPT). Anything else is rejected, including statements
   sqlglot cannot model (they come back as ``Command``).
2. The connection is opened with ``mode=ro`` (the file handle itself is read-only).
3. ``PRAGMA query_only = ON``.
4. ``sqlite3`` authorizer: only SELECT / READ / FUNCTION / RECURSIVE actions are allowed.
5. Progress handler aborts statements that exceed the timeout.
6. ``SQLITE_LIMIT_LENGTH`` caps any single string or blob, which the progress handler cannot
   see (one ``printf`` or ``zeroblob`` call is a single VM instruction).
7. Row cap and a byte cap on the whole result, enforced while fetching, never delegated to the model.
8. SQL text longer than ``MAX_SQL_CHARS`` is rejected before parsing; parser recursion errors
   (deeply nested expressions) are validation errors.

``run_sql`` never raises: it returns ``QueryResult`` or ``QueryError`` so the agent graph
can feed execution errors back to the model as data.
"""

from __future__ import annotations

import re
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import sqlglot
from pydantic import BaseModel
from sqlglot import exp
from sqlglot.errors import ParseError

# --------------------------------------------------------------------------- results


class QueryResult(BaseModel):
    ok: Literal[True] = True
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float
    pii_columns: list[int] = []  # result columns derived from personal data (filled by the agent)


class QueryError(BaseModel):
    ok: Literal[False] = False
    kind: Literal["validation", "execution", "timeout"]
    message: str
    sql: str


QueryOutcome = QueryResult | QueryError

# ------------------------------------------------------------------------ validation

_ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (exp.Select, exp.SetOperation)
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Merge,
    exp.Command,
    exp.Pragma,
    exp.Attach,
    exp.Detach,
    exp.TruncateTable,
)
_FORBIDDEN_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"})

READ_ONLY_MESSAGE = "Só posso consultar dados (SELECT). Alterar, apagar ou criar dados não é permitido."
TOO_BIG_MESSAGE = (
    "O resultado ficou grande demais (limite de {limit}). Agregue, filtre ou devolva menos colunas de texto."
)

MAX_SQL_CHARS = 8_000
MAX_VALUE_BYTES = 1_000_000  # any single string or blob
MAX_RESULT_BYTES = 8_000_000  # the whole fetched result


def validate_sql(sql: str) -> QueryError | None:
    """Return ``None`` if ``sql`` is a single read-only query, else a validation error."""
    text = sql.strip()
    if not text:
        return QueryError(kind="validation", message="A consulta está vazia.", sql=sql)
    if len(text) > MAX_SQL_CHARS:
        return QueryError(
            kind="validation", message=f"A consulta é longa demais (limite de {MAX_SQL_CHARS} caracteres).", sql=sql
        )

    try:
        statements = [s for s in sqlglot.parse(text, read="sqlite") if s is not None]
    except ParseError as err:
        first_line = str(err).splitlines()[0] if str(err) else "erro de sintaxe"
        return QueryError(kind="validation", message=f"SQL inválido: {first_line}", sql=sql)
    except RecursionError:
        return QueryError(kind="validation", message="SQL inválido: expressão aninhada demais.", sql=sql)

    if len(statements) != 1:
        return QueryError(
            kind="validation",
            message="Envie exatamente uma instrução SQL por vez (sem ';' separando comandos).",
            sql=sql,
        )

    root = statements[0]
    if not isinstance(root, _ALLOWED_ROOTS):
        return QueryError(kind="validation", message=READ_ONLY_MESSAGE, sql=sql)

    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            return QueryError(kind="validation", message=READ_ONLY_MESSAGE, sql=sql)
        if isinstance(node, exp.Func):
            name = (node.sql_name() if not isinstance(node, exp.Anonymous) else str(node.this)).lower()
            if name in _FORBIDDEN_FUNCTIONS:
                return QueryError(kind="validation", message=f"Função não permitida: {name}.", sql=sql)
            if name in _FORMAT_FUNCTIONS and not _safe_format(node):
                return QueryError(
                    kind="validation",
                    message=f"{name}: use um formato literal, sem '*' e com largura de até 3 dígitos.",
                    sql=sql,
                )
    return None


_FORMAT_FUNCTIONS = frozenset({"printf", "format"})
_WIDE_SPEC = re.compile(r"%[-+ #0,!]*(\*|\d{4,})|%[-+ #0,!]*\d*\.(\*|\d{4,})")


def _safe_format(node: exp.Func) -> bool:
    """A literal format string with no ``*`` and no width or precision above 999.

    One ``printf('%.*c', 1000000000, 'x')`` is a single VM instruction, so the progress handler
    never sees it run (feedback 2026-11-14, 2.2).
    """
    first = node.this if isinstance(node, exp.Format) else (node.expressions[0] if node.expressions else None)
    if not isinstance(first, exp.Literal) or not first.is_string:
        return False
    return _WIDE_SPEC.search(first.this) is None


# ------------------------------------------------------------------------ connection

_ALLOWED_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
)


def _authorizer(action: int, arg1: str | None, _arg2: str | None, _db: str | None, _trigger: str | None) -> int:
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and arg1 is not None and arg1.lower() in _FORBIDDEN_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def readonly_uri(db_path: Path) -> str:
    """SQLite URI that opens ``db_path`` read-only. ``as_uri`` percent-encodes '#', '?' and '%' and
    handles Windows drive paths; a raw ``file:{path}`` would cut the path at '?' and open another file."""
    return f"{db_path.resolve().as_uri()}?mode=ro"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        # mode=ro would fail anyway, but a clear message helps the operator.
        raise sqlite3.OperationalError(f"banco não encontrado: {db_path}")
    conn = sqlite3.connect(readonly_uri(db_path), uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    return conn


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Read-only connection with the engine-level authorizer installed.

    Exposed so tests can prove the engine blocks writes even when AST validation is bypassed.
    """
    conn = _connect_ro(db_path)
    conn.set_authorizer(_authorizer)
    return conn


# ------------------------------------------------------------------------- execution


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return f"<blob {len(value)} bytes>"
    return value


def _byte_size(value: Any) -> int:
    if isinstance(value, str | bytes):
        return len(value)
    return 8


def run_sql(sql: str, db_path: Path, *, max_rows: int = 1000, timeout_s: float = 5.0) -> QueryOutcome:
    """Validate and execute a read-only query. Never raises."""
    invalid = validate_sql(sql)
    if invalid is not None:
        return invalid

    started = time.monotonic()
    deadline = started + timeout_s
    timed_out = False

    def _progress() -> int:
        nonlocal timed_out
        if time.monotonic() > deadline:
            timed_out = True
            return 1  # non-zero aborts the running statement
        return 0

    try:
        conn = open_readonly(db_path)
    except sqlite3.Error as err:
        return QueryError(kind="execution", message=f"Não foi possível abrir o banco: {err}", sql=sql)

    try:
        conn.set_progress_handler(_progress, 1000)
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        fetched: list[tuple[Any, ...]] = []
        size = 0
        while len(fetched) <= max_rows:
            batch = cursor.fetchmany(min(100, max_rows + 1 - len(fetched)))
            if not batch:
                break
            size += sum(_byte_size(v) for row in batch for v in row)
            if size > MAX_RESULT_BYTES:
                return QueryError(kind="execution", message=TOO_BIG_MESSAGE.format(limit="8 MB"), sql=sql)
            fetched.extend(batch)
    except sqlite3.Error as err:
        if isinstance(err, sqlite3.DataError) or "too big" in str(err).lower():
            return QueryError(kind="execution", message=TOO_BIG_MESSAGE.format(limit="1 MB por valor"), sql=sql)
        if timed_out:
            return QueryError(
                kind="timeout",
                message=f"A consulta excedeu o tempo limite de {timeout_s:g}s. Simplifique ou agregue mais.",
                sql=sql,
            )
        if "not authorized" in str(err).lower():
            return QueryError(kind="validation", message=READ_ONLY_MESSAGE, sql=sql)
        return QueryError(kind="execution", message=f"Erro do SQLite: {err}", sql=sql)
    finally:
        conn.close()

    truncated = len(fetched) > max_rows
    rows = [[_jsonable(v) for v in row] for row in fetched[:max_rows]]
    return QueryResult(
        sql=sql,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=round((time.monotonic() - started) * 1000, 2),
    )


# ------------------------------------------------------------------- schema discovery

ColumnKind = Literal["identifier", "foreign_key", "boolean", "date", "categorical", "integer", "real", "text"]


class ColumnInfo(BaseModel):
    name: str
    declared_type: str
    kind: ColumnKind
    distinct_count: int
    null_count: int
    references: str | None = None  # "table.column" for foreign keys
    values: list[str] | None = None  # only for categorical columns
    min_value: str | float | int | None = None  # dates and numbers
    max_value: str | float | int | None = None


class TableInfo(BaseModel):
    name: str
    row_count: int
    columns: list[ColumnInfo]


class SchemaInfo(BaseModel):
    tables: list[TableInfo]
    reference_date: str | None  # latest date found in any date column

    def table(self, name: str) -> TableInfo | None:
        return next((t for t in self.tables if t.name == name), None)

    def has_column(self, table: str, column: str) -> bool:
        info = self.table(table)
        return info is not None and any(c.name == column for c in info.columns)

    def to_prompt(self, unreliable: frozenset[str] = frozenset()) -> str:
        """Compact, model-friendly description. Contains no sample rows.

        ``unreliable`` (``table.column``) are flagged inline so the model never picks them.
        """
        lines: list[str] = []
        for table in self.tables:
            lines.append(f"Tabela {table.name} ({table.row_count} linhas)")
            for col in table.columns:
                flag = " [NÃO CONFIÁVEL, NÃO USAR]" if f"{table.name}.{col.name}" in unreliable else ""
                lines.append(f"  - {col.name} {col.declared_type or '?'}: {_describe(col)}{flag}")
        if self.reference_date:
            lines.append(f"Data mais recente no banco (data de referência): {self.reference_date}")
        return "\n".join(lines)


def _describe(col: ColumnInfo) -> str:
    if col.kind == "identifier":
        return "chave primária"
    if col.kind == "foreign_key":
        return f"chave estrangeira → {col.references}"
    if col.kind == "boolean":
        return "booleano 0/1"
    if col.kind == "date":
        return f"data TEXT 'YYYY-MM-DD', de {col.min_value} a {col.max_value}"
    if col.kind == "categorical":
        quoted = ", ".join(f"'{v}'" for v in col.values or [])
        return f"categórica, valores exatos: {quoted}"
    if col.kind in ("integer", "real"):
        return f"numérica, de {col.min_value} a {col.max_value}"
    return f"texto livre ({col.distinct_count} valores distintos)"


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


_DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]*"


def _discover_column(
    conn: sqlite3.Connection,
    table: str,
    row_count: int,
    pragma_row: tuple[Any, ...],
    fks: dict[str, str],
    max_categorical: int,
) -> ColumnInfo:
    _cid, name, declared, _notnull, _default, pk = pragma_row
    t, c = _q(table), _q(name)
    distinct, nulls, mn, mx = conn.execute(
        f"SELECT COUNT(DISTINCT {c}), SUM({c} IS NULL), MIN({c}), MAX({c}) FROM {t}"
    ).fetchone()
    base = {"name": name, "declared_type": declared, "distinct_count": distinct, "null_count": nulls or 0}

    if pk:
        return ColumnInfo(kind="identifier", **base)
    if name in fks:
        return ColumnInfo(kind="foreign_key", references=fks[name], **base)

    non_null = row_count - (nulls or 0)
    numeric = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE typeof({c}) IN ('integer','real')").fetchone()[0]
    if non_null and numeric == non_null:
        bools = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {c} IN (0, 1)").fetchone()[0]
        if bools == non_null:
            return ColumnInfo(kind="boolean", **base)
        is_int = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE typeof({c}) = 'integer'").fetchone()[0]
        kind: ColumnKind = "integer" if is_int == non_null else "real"
        return ColumnInfo(kind=kind, min_value=mn, max_value=mx, **base)

    if non_null:
        dates = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {c} GLOB ?", (_DATE_GLOB,)).fetchone()[0]
        if dates == non_null:
            return ColumnInfo(kind="date", min_value=mn, max_value=mx, **base)

    # Low cardinality, and never "one value per row" (avoids exposing unique values such as names).
    if 0 < distinct <= max_categorical and distinct <= row_count * 0.5:
        values = [str(r[0]) for r in conn.execute(f"SELECT DISTINCT {c} FROM {t} WHERE {c} IS NOT NULL ORDER BY 1")]
        return ColumnInfo(kind="categorical", values=values, **base)
    return ColumnInfo(kind="text", **base)


@lru_cache(maxsize=8)
def _discover_cached(
    db_path: str, _mtime_ns: int, max_categorical: int, exclude_from_reference: frozenset[str]
) -> SchemaInfo:
    conn = _connect_ro(Path(db_path))
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables: list[TableInfo] = []
        dates: list[str] = []
        for name in names:
            row_count = conn.execute(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0]
            fks = {r[3]: f"{r[2]}.{r[4]}" for r in conn.execute(f"PRAGMA foreign_key_list({_q(name)})")}
            columns = [
                _discover_column(conn, name, row_count, row, fks, max_categorical)
                for row in conn.execute(f"PRAGMA table_info({_q(name)})").fetchall()
            ]
            dates += [
                str(col.max_value)
                for col in columns
                if col.kind == "date" and col.max_value and f"{name}.{col.name}" not in exclude_from_reference
            ]
            tables.append(TableInfo(name=name, row_count=row_count, columns=columns))
        return SchemaInfo(tables=tables, reference_date=max(dates, default=None))
    finally:
        conn.close()


def discover_schema(
    db_path: Path, *, max_categorical: int = 30, exclude_from_reference: frozenset[str] = frozenset()
) -> SchemaInfo:
    """Introspect the database. Cached per (path, modification time, options).

    ``exclude_from_reference`` lists ``table.column`` date columns that must not drive the
    reference date (e.g. denormalized columns the semantic layer marks as unreliable).
    """
    resolved = db_path.resolve()
    return _discover_cached(str(resolved), resolved.stat().st_mtime_ns, max_categorical, exclude_from_reference)
