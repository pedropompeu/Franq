"""LangGraph agent: context → decide → (execute ↺ | finalize | refuse).

Single agent with clearly separated nodes (no multi-agent: saves quota and keeps the flow
auditable). Limits: at most ``max_steps`` LLM decisions and ``max_corrections`` failed
queries per question.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.agent.models import AgentDecision, AgentResponse, ChartSpec, Step, TokenUsage
from src.agent.notices import compute_notices
from src.agent.prompts import build_context, build_messages
from src.config import Settings
from src.db.tools import QueryError, QueryResult, SchemaInfo, discover_schema, run_sql
from src.llm import LLMError, StructuredLLM
from src.observability import Tracer
from src.privacy import Pseudonymizer, load_pseudonymizer
from src.progress import Listener, Progress, ProgressKind, listening, report
from src.run_context import RunInfo, run_scope
from src.semantic.dictionary import SemanticDictionary, load_dictionary, validate_against_schema
from src.versioning import prompt_version

REFUSAL_FALLBACK = "Não posso atender a esse pedido. Só respondo perguntas consultando os dados."


class AgentState(TypedDict):
    question: str
    context: str
    steps: list[Step]
    results: dict[int, QueryResult]
    decision: AgentDecision | None
    corrections: int
    llm_calls: int
    cache_hits: int
    model: str
    usage: TokenUsage
    response: AgentResponse | None


@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 6
    max_corrections: int = 3


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.split()).rstrip(";").strip().lower()


def _require_decision(state: AgentState) -> AgentDecision:
    decision = state["decision"]
    if decision is None:  # unreachable by routing; guards against graph wiring mistakes
        raise RuntimeError("node reached without an LLM decision")
    return decision


def resolve_chart(decision: AgentDecision, result: QueryResult | None) -> ChartSpec:
    """Validate the model's chart proposal against the actual result; fall back to a table."""
    if result is None or result.row_count == 0:
        return ChartSpec(type="table")
    cols = set(result.columns)
    if decision.chart_type == "metric":
        if result.row_count == 1 and (decision.y_column in cols or len(result.columns) == 1):
            return ChartSpec(type="metric", y=decision.y_column if decision.y_column in cols else result.columns[0])
        return ChartSpec(type="table")
    if decision.chart_type in ("bar", "line"):
        if decision.x_column in cols and decision.y_column in cols and decision.x_column != decision.y_column:
            series = decision.series_column if decision.series_column in cols else None
            return ChartSpec(type=decision.chart_type, x=decision.x_column, y=decision.y_column, series=series)
        return ChartSpec(type="table")
    return ChartSpec(type="table")


def _llm_phase(steps: list[Step]) -> ProgressKind:
    """What the next LLM call is for, from the last thing that happened."""
    if not steps:
        return "planning"
    return "fixing" if steps[-1].kind == "query_error" else "reviewing"


class Assistant:
    """Public entry point: ``Assistant(llm, settings).ask(question) -> AgentResponse``."""

    def __init__(
        self,
        llm: StructuredLLM,
        settings: Settings,
        *,
        limits: AgentLimits | None = None,
        tracer: Tracer | None = None,
        dictionary: SemanticDictionary | None = None,
        privacy: Pseudonymizer | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.limits = limits or AgentLimits()
        self.tracer = tracer
        self.dictionary = dictionary or load_dictionary(settings.dictionary_path)
        # Personal data never leaves for the LLM: see src/privacy.py.
        self.privacy = privacy or load_pseudonymizer(settings.db_path, self.dictionary.pii_columns())
        self.schema_warnings: list[str] = []
        self.schema: SchemaInfo | None = None
        self.prompt_version = prompt_version(settings.dictionary_path)
        self._graph = self._build()

    # ------------------------------------------------------------------ nodes

    def _load_context(self, state: AgentState) -> dict[str, object]:
        report(Progress("context"))
        unreliable = self.dictionary.unreliable_columns()
        schema = discover_schema(
            self.settings.db_path,
            max_categorical=self.settings.max_categorical_values,
            exclude_from_reference=unreliable,
        )
        self.schema = schema
        self.schema_warnings = validate_against_schema(self.dictionary, schema)
        context = build_context(schema.to_prompt(unreliable), self.dictionary.to_prompt())
        return {"context": self.privacy.mask(context)}

    def _decide(self, state: AgentState) -> dict[str, object]:
        steps = state["steps"]
        if state["llm_calls"] >= self.limits.max_steps:
            return {"response": self._fail(state, f"Limite de {self.limits.max_steps} passos atingido sem resposta.")}
        report(Progress(_llm_phase(steps)))
        messages = build_messages(
            state["question"],
            state["context"],
            steps,
            state["results"],
            max_rows=self.settings.max_rows_for_llm,
            remaining_steps=self.limits.max_steps - state["llm_calls"],
            privacy=self.privacy,
        )
        try:
            reply = self.llm.decide(messages)
        except LLMError as err:
            failed = [*steps, Step(index=len(steps) + 1, kind="llm_error", error=str(err))]
            return {
                "steps": failed,
                "llm_calls": state["llm_calls"] + 1,
                "response": self._fail(
                    {**state, "steps": failed, "llm_calls": state["llm_calls"] + 1},
                    "Não consegui consultar o modelo de linguagem.",
                ),
            }
        # The model writes tokens like '[nome_3]'; the SQL runs (and is shown) with the real value.
        decision = reply.decision.model_copy(update={"sql": self.privacy.unmask_sql(reply.decision.sql)})
        return {
            "decision": decision,
            "llm_calls": state["llm_calls"] + 1,
            "cache_hits": state["cache_hits"] + int(reply.cached),
            "model": reply.model,
            "usage": state["usage"].add(reply.usage),
        }

    def _execute(self, state: AgentState) -> dict[str, object]:
        decision = _require_decision(state)
        return self._run_step(state, decision)

    def _run_step(self, state: AgentState, decision: AgentDecision) -> dict[str, object]:
        steps, results = list(state["steps"]), dict(state["results"])
        index = len(steps) + 1
        outcome = run_sql(
            decision.sql,
            self.settings.db_path,
            max_rows=self.settings.max_rows,
            timeout_s=self.settings.sql_timeout_s,
        )
        update: dict[str, object] = {}
        if isinstance(outcome, QueryError):
            report(Progress("query_failed"))
            steps.append(
                Step(index=index, kind="query_error", thought=decision.thought, sql=decision.sql, error=outcome.message)
            )
            corrections = state["corrections"] + 1
            update["corrections"] = corrections
            if corrections > self.limits.max_corrections:
                update["response"] = self._fail(
                    {**state, "steps": steps, "corrections": corrections},
                    f"A consulta continuou falhando após {self.limits.max_corrections} correções.",
                )
        else:
            report(Progress("query_done", rows=outcome.row_count, elapsed_ms=outcome.elapsed_ms))
            steps.append(
                Step(
                    index=index,
                    kind="query",
                    thought=decision.thought,
                    sql=decision.sql,
                    row_count=outcome.row_count,
                    columns=outcome.columns,
                    elapsed_ms=outcome.elapsed_ms,
                )
            )
            results[index] = outcome
        update.update({"steps": steps, "results": results})
        return update

    def _finalize(self, state: AgentState) -> dict[str, object]:
        decision = _require_decision(state)
        result: QueryResult | None = None
        if decision.sql.strip():
            wanted = _normalize_sql(decision.sql)
            matches = [s.index for s in state["steps"] if s.kind == "query" and _normalize_sql(s.sql) == wanted]
            if not matches:
                # Observe before answering: run it and let the model read the result first.
                return self._run_step(state, decision)
            result = state["results"][matches[-1]]
        steps = [*state["steps"], Step(index=len(state["steps"]) + 1, kind="answer", thought=decision.thought)]
        return {
            "steps": steps,
            "response": AgentResponse(
                question=state["question"],
                status="answered",
                answer=decision.answer,
                assumptions=decision.assumptions,
                chart=resolve_chart(decision, result),
                data=result,
                steps=steps,
                corrections=state["corrections"],
                llm_calls=state["llm_calls"],
                usage=state["usage"],
            ),
        }

    def _refuse(self, state: AgentState) -> dict[str, object]:
        decision = _require_decision(state)
        steps = [*state["steps"], Step(index=len(state["steps"]) + 1, kind="refusal", thought=decision.thought)]
        return {
            "response": AgentResponse(
                question=state["question"],
                status="refused",
                answer=decision.answer or REFUSAL_FALLBACK,
                steps=steps,
                llm_calls=state["llm_calls"],
                usage=state["usage"],
            )
        }

    def _fail(self, state: AgentState, reason: str) -> AgentResponse:
        steps = [*state["steps"], Step(index=len(state["steps"]) + 1, kind="limit", error=reason)]
        return AgentResponse(
            question=state["question"],
            status="failed",
            answer=f"Não consegui responder com segurança. {reason}",
            steps=steps,
            corrections=state["corrections"],
            llm_calls=state["llm_calls"],
            usage=state["usage"],
        )

    # ---------------------------------------------------------------- routing

    @staticmethod
    def _after_decide(state: AgentState) -> Literal["execute", "finalize", "refuse", "__end__"]:
        if state["response"] is not None or state["decision"] is None:
            return "__end__"
        return {"run_query": "execute", "final_answer": "finalize", "refuse": "refuse"}[  # type: ignore[return-value]
            state["decision"].action
        ]

    @staticmethod
    def _loop_or_end(state: AgentState) -> Literal["decide", "__end__"]:
        return "__end__" if state["response"] is not None else "decide"

    def _build(self) -> CompiledStateGraph:  # type: ignore[type-arg]
        graph = StateGraph(AgentState)
        graph.add_node("load_context", self._load_context)
        graph.add_node("decide", self._decide)
        graph.add_node("execute", self._execute)
        graph.add_node("finalize", self._finalize)
        graph.add_node("refuse", self._refuse)
        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "decide")
        graph.add_conditional_edges("decide", self._after_decide)
        graph.add_conditional_edges("execute", self._loop_or_end)
        graph.add_conditional_edges("finalize", self._loop_or_end)
        graph.add_edge("refuse", END)
        return graph.compile()

    # ------------------------------------------------------------------ entry

    def ask(self, question: str, *, on_progress: Listener | None = None) -> AgentResponse:
        """Answer ``question``; ``on_progress`` receives each stage as it starts (see ``src/progress.py``)."""
        started = time.monotonic()
        initial: AgentState = {
            "question": question,
            "context": "",
            "steps": [],
            "results": {},
            "decision": None,
            "corrections": 0,
            "llm_calls": 0,
            "cache_hits": 0,
            "model": "",
            "usage": TokenUsage(),
            "response": None,
        }
        trace_id = uuid.uuid4().hex[:12]
        with listening(on_progress), run_scope(RunInfo(trace_id, self.prompt_version)):
            final = self._graph.invoke(initial, {"recursion_limit": 4 * self.limits.max_steps + 10})
        response: AgentResponse = final["response"]
        if response.status == "answered" and response.data is not None and self.schema is not None:
            notices = compute_notices(response.data.sql, response.data, self.schema, self.settings)
        else:
            notices = []
        response = response.model_copy(
            update={
                "notices": notices,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "model": final["model"] or self.llm.model_name,
                "cache_hits": final["cache_hits"],
                "trace_id": trace_id,
                "prompt_version": self.prompt_version,
            }
        )
        if self.tracer is not None:
            self.tracer.write(self._masked(response))
        return self._unmasked(response)

    def _unmasked(self, response: AgentResponse) -> AgentResponse:
        """What the user sees: the model's tokens turned back into real values."""
        p = self.privacy
        return response.model_copy(
            update={
                "answer": p.unmask(response.answer),
                "assumptions": [p.unmask(a) for a in response.assumptions],
                "steps": [s.model_copy(update={"thought": p.unmask(s.thought)}) for s in response.steps],
            }
        )

    def _masked(self, response: AgentResponse) -> AgentResponse:
        """What the trace keeps: no personal values in the question or the SQL."""
        p = self.privacy
        return response.model_copy(
            update={
                "question": p.mask(response.question),
                "steps": [
                    s.model_copy(update={"sql": p.mask(s.sql), "error": p.mask(s.error) if s.error else s.error})
                    for s in response.steps
                ],
            }
        )
