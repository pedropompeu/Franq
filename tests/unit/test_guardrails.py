"""Attack matrix for run_sql: every layer is tested on its own and end to end."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from src.db.tools import QueryError, QueryResult, open_readonly, run_sql, validate_sql

WRITE_ATTEMPTS = [
    "DROP TABLE clientes",
    "DELETE FROM compras",
    "UPDATE clientes SET nome = 'x'",
    "INSERT INTO compras (cliente_id, valor) VALUES (1, 10)",
    "REPLACE INTO compras (id) VALUES (1)",
    "CREATE TABLE t (a INT)",
    "ALTER TABLE clientes ADD COLUMN x INT",
    "ATTACH DATABASE ':memory:' AS evil",
    "DETACH evil",
    "PRAGMA writable_schema = 1",
    "VACUUM",
    "REINDEX",
    "EXPLAIN SELECT 1",
    "WITH x AS (SELECT 1) DELETE FROM compras",
    "-- só um comentário\nDROP TABLE clientes",
    "SELECT load_extension('evil')",
]

MULTI_STATEMENT = [
    "SELECT 1; DROP TABLE clientes",
    "SELECT 1; SELECT 2",
    "SELECT * FROM clientes;\nDELETE FROM clientes",
]


@pytest.mark.parametrize("sql", WRITE_ATTEMPTS + MULTI_STATEMENT)
def test_ast_validation_rejects_non_select(sql: str) -> None:
    error = validate_sql(sql)
    assert isinstance(error, QueryError)
    assert error.kind == "validation"


@pytest.mark.parametrize("sql", WRITE_ATTEMPTS + MULTI_STATEMENT)
def test_run_sql_blocks_and_never_raises(sql: str, db_path: Path) -> None:
    outcome = run_sql(sql, db_path)
    assert isinstance(outcome, QueryError)
    assert outcome.kind == "validation"
    assert "Traceback" not in outcome.message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1;",
        "SELECT 1 /* ; DROP TABLE clientes */",
        "SELECT canal FROM compras UNION SELECT canal FROM suporte",
        "WITH a AS (SELECT cliente_id FROM compras) SELECT COUNT(*) FROM a",
        "SELECT strftime('%Y-%m', data_compra) AS mes, COUNT(*) FROM compras GROUP BY mes",
        "SELECT * FROM compras WHERE data_compra >= date('2025-07-22', '-1 year')",
    ],
)
def test_valid_reads_pass_validation(sql: str) -> None:
    assert validate_sql(sql) is None


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM compras",
        "DROP TABLE clientes",
        "INSERT INTO compras (cliente_id) VALUES (1)",
        "PRAGMA query_only = OFF",
        "ATTACH DATABASE ':memory:' AS evil",
        "CREATE TEMP TABLE t (a INT)",
    ],
)
def test_engine_blocks_writes_even_without_ast_layer(statement: str, db_path: Path) -> None:
    """Bypass validate_sql entirely: the read-only connection itself must refuse."""
    conn = open_readonly(db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(statement)
    finally:
        conn.close()


def test_empty_and_garbage_input(db_path: Path) -> None:
    for sql in ["", "   ", "SELEC 1", "isto não é sql"]:
        outcome = run_sql(sql, db_path)
        assert isinstance(outcome, QueryError)


def test_execution_error_is_returned_as_data(db_path: Path) -> None:
    outcome = run_sql("SELECT coluna_que_nao_existe FROM compras", db_path)
    assert isinstance(outcome, QueryError)
    assert outcome.kind == "execution"
    assert "no such column" in outcome.message


def test_timeout_aborts_runaway_query(db_path: Path) -> None:
    # The query never ends, so the only possible outcome is the timeout: a slow machine makes the
    # test take longer, never pass or fail by luck (feedback 2026-11-14, 4.6).
    runaway = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) FROM r"
    outcome = run_sql(runaway, db_path, timeout_s=0.2)
    assert isinstance(outcome, QueryError)
    assert outcome.kind == "timeout"


def test_row_cap_is_enforced_by_the_system(db_path: Path) -> None:
    outcome = run_sql("SELECT * FROM compras", db_path, max_rows=10)
    assert isinstance(outcome, QueryResult)
    assert outcome.row_count == 10
    assert outcome.truncated is True


def test_small_result_is_not_flagged_truncated(db_path: Path) -> None:
    outcome = run_sql("SELECT COUNT(*) AS n FROM clientes", db_path)
    assert isinstance(outcome, QueryResult)
    assert outcome.rows == [[100]]
    assert outcome.truncated is False


def test_missing_database_returns_error(tmp_path: Path) -> None:
    outcome = run_sql("SELECT 1", tmp_path / "nao_existe.db")
    assert isinstance(outcome, QueryError)
    assert outcome.kind == "execution"


def test_regression_question_1_reference_sql(db_path: Path) -> None:
    """Reference answer for 'top 5 states by customers who bought via app in May'."""
    sql = """
        SELECT c.estado, COUNT(DISTINCT c.id) AS n
        FROM compras p JOIN clientes c ON c.id = p.cliente_id
        WHERE p.canal = 'App' AND strftime('%Y-%m', p.data_compra) = '2025-05'
        GROUP BY c.estado
        ORDER BY n DESC, c.estado
        LIMIT 5
    """
    outcome = run_sql(sql, db_path)
    assert isinstance(outcome, QueryResult)
    assert outcome.rows == [
        ["São Paulo", 6],
        ["Minas Gerais", 3],
        ["Santa Catarina", 3],
        ["Alagoas", 2],
        ["Espírito Santo", 2],
    ]


# ------------------------------------------------- resource limits (feedback 2026-11-14, 2.2 and 2.4)

HEAVY_SCALARS = [
    "SELECT length(printf('%.*c', 1000000000, 'x'))",  # a single huge string, one VM instruction
    "SELECT length(zeroblob(2000000000))",
    "SELECT length(replace(hex(zeroblob(400000)), '0', '00000'))",
]


@pytest.mark.parametrize("sql", HEAVY_SCALARS)
def test_huge_scalar_values_fail_fast(sql: str, db_path: Path) -> None:
    started = time.monotonic()
    result = run_sql(sql, db_path, timeout_s=5)
    assert isinstance(result, QueryError)
    assert result.kind in {"validation", "execution"}  # printf is caught by the AST, the rest by the engine
    assert time.monotonic() - started < 2


def test_result_size_is_capped_in_bytes(db_path: Path) -> None:
    sql = (
        "WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r WHERE i < 400) "
        "SELECT hex(zeroblob(250000)) FROM r"
    )
    result = run_sql(sql, db_path, max_rows=1000, timeout_s=5)
    assert isinstance(result, QueryError)
    assert "grande demais" in result.message


def test_deeply_nested_sql_is_a_validation_error(db_path: Path) -> None:
    sql = "SELECT " + "(" * 500 + "1" + ")" * 500
    assert isinstance(validate_sql(sql), QueryError)
    result = run_sql(sql, db_path)
    assert isinstance(result, QueryError)
    assert result.kind == "validation"


def test_oversized_sql_text_is_rejected(db_path: Path) -> None:
    sql = "SELECT 1 WHERE 1 IN (" + ", ".join(["1"] * 5000) + ")"
    result = run_sql(sql, db_path)
    assert isinstance(result, QueryError)
    assert result.kind == "validation"
    assert "longa" in result.message


@pytest.mark.parametrize(
    ("sql", "allowed"),
    [
        ("SELECT printf('%.2f', 1.5)", True),
        ("SELECT printf('%5d - %s', 1, 'a')", True),
        ("SELECT format('%.1f%%', 12.34)", True),
        ("SELECT printf('%.*c', 5, 'x')", False),
        ("SELECT printf('%1000000c', 'x')", False),
        ("SELECT printf(nome, 1) FROM clientes", False),
    ],
)
def test_printf_needs_a_small_literal_format(sql: str, allowed: bool) -> None:
    assert (validate_sql(sql) is None) is allowed
