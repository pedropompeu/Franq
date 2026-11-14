"""Data contracts of the agent: what the model decides and what the app receives."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.db.tools import QueryResult

ChartType = Literal["table", "bar", "line", "metric"]


class AgentDecision(BaseModel):
    """Flat structured output requested from the LLM at every step."""

    thought: str = Field(description="Raciocínio curto (1-2 frases, pt-BR) sobre o próximo passo. Visível ao usuário.")
    action: Literal["run_query", "final_answer", "refuse"] = Field(
        description="run_query: executar uma consulta para investigar. final_answer: responder. refuse: recusar."
    )
    sql: str = Field(
        default="",
        description="run_query: a consulta SQLite (um único SELECT). final_answer: a consulta cujo resultado "
        "sustenta a resposta e será exibida (deve ter sido executada antes). refuse: vazio.",
    )
    answer: str = Field(
        default="",
        description="final_answer/refuse: resposta ao usuário em pt-BR, citando os números do resultado.",
    )
    chart_type: ChartType = Field(
        default="table",
        description="table para listas; bar para comparar categorias; line para série temporal; "
        "metric para um único número.",
    )
    x_column: str = Field(default="", description="Coluna do resultado no eixo X (bar/line).")
    y_column: str = Field(default="", description="Coluna numérica do resultado no eixo Y (bar/line/metric).")
    series_column: str = Field(default="", description="Opcional: coluna que separa séries (ex.: uma linha por canal).")
    assumptions: list[str] = Field(
        default_factory=list,
        description="Premissas adotadas para resolver ambiguidades (ex.: qual período, qual denominador).",
    )


class ChartSpec(BaseModel):
    type: ChartType
    x: str | None = None
    y: str | None = None
    series: str | None = None


StepKind = Literal["query", "query_error", "answer", "refusal", "llm_error", "limit"]


class Step(BaseModel):
    index: int
    kind: StepKind
    thought: str = ""
    sql: str = ""
    row_count: int | None = None
    columns: list[str] = Field(default_factory=list)
    error: str | None = None
    elapsed_ms: float = 0.0


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


NoticeKind = Literal["tie_at_cut", "partial_period"]


class Notice(BaseModel):
    """A warning computed in code from the SQL and the data, never by the LLM."""

    kind: NoticeKind
    text: str


class AgentResponse(BaseModel):
    question: str
    status: Literal["answered", "refused", "failed"]
    answer: str
    assumptions: list[str] = Field(default_factory=list)
    notices: list[Notice] = Field(default_factory=list)
    chart: ChartSpec = Field(default_factory=lambda: ChartSpec(type="table"))
    data: QueryResult | None = None
    steps: list[Step] = Field(default_factory=list)
    corrections: int = 0
    llm_calls: int = 0
    cache_hits: int = 0  # decisions served from the local cache (no API call)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    model: str = ""
    trace_id: str = ""
    prompt_version: str = ""  # see src/versioning.py


TurnStatus = Literal["answered", "refused", "failed"]


class Turn(BaseModel):
    """One finished question of a conversation, as the next prompt sees it.

    Everything is masked (``[nome_N]`` tokens instead of personal values) and there are no result
    rows: the model must query again for numbers, and the conversation store never keeps what the user saw.
    """

    question: str
    sql: str = ""
    answer: str
    status: TurnStatus
