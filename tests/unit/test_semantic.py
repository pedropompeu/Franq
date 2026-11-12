from __future__ import annotations

from pathlib import Path

import yaml

from src.db.tools import discover_schema
from src.semantic.dictionary import load_dictionary, validate_against_schema
from tests.conftest import DICTIONARY


def test_dictionary_matches_real_schema(db_path: Path) -> None:
    dictionary = load_dictionary(DICTIONARY)
    assert validate_against_schema(dictionary, discover_schema(db_path)) == []


def test_drift_is_reported(db_path: Path, tmp_path: Path) -> None:
    raw = yaml.safe_load(DICTIONARY.read_text(encoding="utf-8"))
    raw["tables"]["compras"]["columns"]["coluna_fantasma"] = {"note": "não existe"}
    raw["metrics"][0]["columns"].append("tabela_fantasma.x")
    drifted = tmp_path / "dictionary.yaml"
    drifted.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    warnings = validate_against_schema(load_dictionary(drifted), discover_schema(db_path))
    assert any("compras.coluna_fantasma" in w for w in warnings)
    assert any("tabela_fantasma" in w for w in warnings)


def test_unreliable_columns_are_flagged() -> None:
    dictionary = load_dictionary(DICTIONARY)
    assert dictionary.unreliable_columns() == {"clientes.valor_total_gasto", "clientes.data_ultima_compra"}
    assert "NÃO CONFIÁVEL" in dictionary.to_prompt()


def test_dictionary_does_not_hardcode_categorical_values() -> None:
    """Value lists belong to dynamic discovery, not to the YAML."""
    raw = yaml.safe_load(DICTIONARY.read_text(encoding="utf-8"))
    for table in raw["tables"].values():
        for column in table.get("columns", {}).values():
            assert "values" not in column
