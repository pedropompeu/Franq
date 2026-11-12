from __future__ import annotations

from pathlib import Path

from src.db.tools import discover_schema

EXPECTED_CATEGORICAL = {
    ("compras", "canal"): ["App", "Loja Física", "Site"],
    ("compras", "categoria"): ["Alimentos", "Eletrônicos", "Livros", "Roupas", "Serviços", "Viagens"],
    ("suporte", "canal"): ["Chat", "E-mail", "Telefone"],
    ("suporte", "tipo_contato"): ["Dúvida", "Reclamação", "Sugestão"],
    ("campanhas_marketing", "canal"): ["E-mail", "SMS", "WhatsApp"],
}


def _column(db_path: Path, table: str, column: str):  # type: ignore[no-untyped-def]
    info = discover_schema(db_path).table(table)
    assert info is not None
    return next(c for c in info.columns if c.name == column)


def test_discovers_all_user_tables_and_skips_internal(db_path: Path) -> None:
    schema = discover_schema(db_path)
    assert [t.name for t in schema.tables] == ["campanhas_marketing", "clientes", "compras", "suporte"]
    assert {t.name: t.row_count for t in schema.tables} == {
        "campanhas_marketing": 248,
        "clientes": 100,
        "compras": 946,
        "suporte": 273,
    }


def test_categorical_values_are_discovered_not_hardcoded(db_path: Path) -> None:
    for (table, column), values in EXPECTED_CATEGORICAL.items():
        col = _column(db_path, table, column)
        assert col.kind == "categorical"
        assert col.values == values


def test_column_kinds(db_path: Path) -> None:
    assert _column(db_path, "suporte", "resolvido").kind == "boolean"
    assert _column(db_path, "campanhas_marketing", "interagiu").kind == "boolean"
    assert _column(db_path, "compras", "data_compra").kind == "date"
    assert _column(db_path, "compras", "valor").kind == "real"
    assert _column(db_path, "clientes", "idade").kind == "integer"
    assert _column(db_path, "compras", "id").kind == "identifier"
    fk = _column(db_path, "compras", "cliente_id")
    assert fk.kind == "foreign_key"
    assert fk.references == "clientes.id"


def test_date_ranges_and_reference_date(db_path: Path) -> None:
    col = _column(db_path, "compras", "data_compra")
    assert (col.min_value, col.max_value) == ("2024-07-22", "2025-07-22")
    assert discover_schema(db_path).reference_date == "2025-07-22"


def test_reference_date_ignores_excluded_columns(db_path: Path) -> None:
    every_date_column_excluded = frozenset(
        {
            "compras.data_compra",
            "suporte.data_contato",
            "campanhas_marketing.data_envio",
            "clientes.data_ultima_compra",
        }
    )
    schema = discover_schema(db_path, exclude_from_reference=every_date_column_excluded)
    assert schema.reference_date is None


def test_unique_text_columns_never_expose_values(db_path: Path) -> None:
    for column in ("nome", "email", "cidade", "profissao"):
        col = _column(db_path, "clientes", column)
        assert col.kind == "text"
        assert col.values is None


def test_prompt_has_no_personal_data(db_path: Path) -> None:
    prompt = discover_schema(db_path).to_prompt()
    assert "@" not in prompt  # no e-mail addresses
    assert "'São Paulo'" in prompt  # low-cardinality values are present
    assert "2025-07-22" in prompt


def test_discovery_is_cached(db_path: Path) -> None:
    assert discover_schema(db_path) is discover_schema(db_path)
