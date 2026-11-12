"""Load the versioned semantic dictionary and check it against the discovered schema."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.db.tools import SchemaInfo


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ColumnNote(_Strict):
    reliable: bool = True
    pii: bool = False
    note: str | None = None


class TableNote(_Strict):
    description: str
    columns: dict[str, ColumnNote] = Field(default_factory=dict)


class Metric(_Strict):
    name: str
    description: str
    rule: str
    columns: list[str]
    ambiguity: str | None = None


class ReferenceDate(_Strict):
    rule: str
    coverage_note: str


class SemanticDictionary(_Strict):
    version: int
    reference_date: ReferenceDate
    tables: dict[str, TableNote]
    metrics: list[Metric]
    conventions: list[str]

    def unreliable_columns(self) -> frozenset[str]:
        """``table.column`` names that must never be used (nor drive the reference date)."""
        return frozenset(
            f"{table}.{column}"
            for table, note in self.tables.items()
            for column, col in note.columns.items()
            if not col.reliable
        )

    def referenced_columns(self) -> set[str]:
        refs = {f"{t}.{c}" for t, note in self.tables.items() for c in note.columns}
        refs |= {col for metric in self.metrics for col in metric.columns}
        return refs

    def to_prompt(self) -> str:
        lines = ["## Regras de negócio", f"- Data de referência: {self.reference_date.rule}"]
        lines.append(f"- Cobertura: {self.reference_date.coverage_note}")
        for table, note in self.tables.items():
            lines.append(f"- Tabela {table}: {note.description}")
            for column, col in note.columns.items():
                flags = []
                if not col.reliable:
                    flags.append("NÃO CONFIÁVEL, NÃO USAR")
                if col.pii:
                    flags.append("dado pessoal, só exibir se a pergunta pedir")
                detail = "; ".join(flags + ([col.note] if col.note else []))
                lines.append(f"  - {table}.{column}: {detail}")
        lines.append("## Métricas")
        for m in self.metrics:
            lines.append(f"- {m.name}: {m.description} Regra: {m.rule}")
            if m.ambiguity:
                lines.append(f"  - Ambiguidade: {m.ambiguity}")
        lines.append("## Convenções")
        lines += [f"- {c}" for c in self.conventions]
        return "\n".join(lines)


def load_dictionary(path: Path) -> SemanticDictionary:
    with path.open(encoding="utf-8") as fh:
        return SemanticDictionary.model_validate(yaml.safe_load(fh))


def validate_against_schema(dictionary: SemanticDictionary, schema: SchemaInfo) -> list[str]:
    """Return human-readable drift warnings (empty list = dictionary matches the database)."""
    missing_tables = sorted({ref.partition(".")[0] for ref in dictionary.referenced_columns()} | set(dictionary.tables))
    warnings = [
        f"Dicionário cita a tabela '{t}', que não existe no banco." for t in missing_tables if schema.table(t) is None
    ]
    for ref in sorted(dictionary.referenced_columns()):
        table, _, column = ref.partition(".")
        if schema.table(table) is not None and not schema.has_column(table, column):
            warnings.append(f"Dicionário cita a coluna '{ref}', que não existe no banco.")
    return warnings
