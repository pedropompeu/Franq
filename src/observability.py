"""Always-on local traces: one JSONL line per question.

Traces never store result rows (the database has names and e-mails): only SQL, row
counts, column names, errors, tokens and latency.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.agent.models import AgentResponse


def trace_record(response: AgentResponse) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "trace_id": response.trace_id,
        "question": response.question,
        "status": response.status,
        "model": response.model,
        "latency_ms": response.latency_ms,
        "llm_calls": response.llm_calls,
        "corrections": response.corrections,
        "usage": response.usage.model_dump(),
        "chart": response.chart.model_dump(),
        "result_rows": response.data.row_count if response.data else None,
        "steps": [
            s.model_dump(include={"index", "kind", "sql", "row_count", "columns", "error", "elapsed_ms"})
            for s in response.steps
        ],
    }


class Tracer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, response: AgentResponse) -> None:
        line = json.dumps(trace_record(response), ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(line) for line in lines if line.strip()]
