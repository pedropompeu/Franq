"""Runtime settings read from environment variables, with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    db_path: Path
    dictionary_path: Path
    max_rows: int  # hard cap on rows returned by run_sql (UI)
    max_rows_for_llm: int  # rows shown to the model when it reads a result
    sql_timeout_s: float
    max_categorical_values: int  # TEXT columns with at most N distinct values are categorical

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            db_path=Path(os.getenv("DB_PATH", PROJECT_ROOT / "data" / "anexo_desafio_1.db")),
            dictionary_path=Path(
                os.getenv("SEMANTIC_DICTIONARY", PROJECT_ROOT / "src" / "semantic" / "dictionary.yaml")
            ),
            max_rows=_int_env("SQL_MAX_ROWS", 1000),
            max_rows_for_llm=_int_env("SQL_MAX_ROWS_LLM", 50),
            sql_timeout_s=_float_env("SQL_TIMEOUT_S", 5.0),
            max_categorical_values=_int_env("SCHEMA_MAX_CATEGORICAL", 30),
        )
