"""Runtime settings read from environment variables, with safe defaults."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path | None = None) -> None:
    """Minimal ``.env`` loader (KEY=VALUE lines). Real environment variables always win."""
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


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
    trace_path: Path  # JSONL trace file (no result rows, no PII)
    price: Price | None = None  # estimated cost per question; no default price on purpose

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
            trace_path=Path(os.getenv("TRACE_PATH", PROJECT_ROOT / "traces" / "traces.jsonl")),
            price=parse_price(raw) if (raw := os.getenv("LLM_PRICE_USD_PER_MTOK")) else None,
        )


@dataclass(frozen=True)
class Price:
    """LLM price in US$ per million tokens, used only to estimate the cost per question."""

    input_per_mtok: float
    output_per_mtok: float


def parse_price(raw: str) -> Price:
    """``"0.30,2.50"`` (input, output, US$ per million tokens) → ``Price``."""
    parts = [part.strip() for part in raw.split(",")]
    try:
        if len(parts) != 2:
            raise ValueError
        values = [float(part) for part in parts]
    except ValueError:
        values = []
    if len(values) != 2 or not all(math.isfinite(v) and v >= 0 for v in values):
        raise ValueError(
            f"LLM_PRICE_USD_PER_MTOK must be 'input,output' in US$ per million tokens (e.g. 0.30,2.50); got {raw!r}"
        )
    return Price(values[0], values[1])
