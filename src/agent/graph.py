"""LangGraph agent: context → decide → (execute ↺ | finalize | refuse) → remember.

Single agent with clearly separated nodes (no multi-agent: saves quota and keeps the flow
auditable). Limits: at most ``max_steps`` LLM decisions and ``max_corrections`` failed
queries per question.

Memory: ``ask`` reads the conversation's earlier turns from a ``ConversationStore``
(``src/agent/memory.py``: masked question, SQL and answer only, bounded by LRU and TTL), passes
them in as ``history`` and stores the finished ``turn`` afterwards. The graph itself keeps no
state between calls (no checkpointer), and questions of one ``thread_id`` run one at a time.
Without a ``thread_id`` nothing is kept.
"""

from __future__ import annotations

import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.agent.memory import ConversationStore
from src.agent.models import AgentDecision, AgentResponse, ChartSpec, Step, TokenUsage, Turn
from src.agent.notices import compute_notices
from src.agent.prompts import build_context, build_messages
from src.config import Settings
from src.db.lineage import pii_derived_columns
from src.db.tools import QueryError, QueryResult, SchemaInfo, discover_schema, run_sql
from src.llm import LLMError, StructuredLLM
from src.observability import Tracer
from src.privacy import Pseudonymizer, load_pseudonymizer, strip_external
from src.progress import Listener, Progress, ProgressKind, listening, report
from src.run_context import RunInfo, run_scope
from src.semantic.dictionary import SemanticDictionary, load_dictionary, validate_against_schema
from src.versioning import prompt_version

REFUSAL_FALLBACK = "Não posso atender a esse pedido. Só respondo perguntas consultando os dados."


class AgentState(TypedDict):
    """One question's state; ``history`` comes in from the conversation, ``turn`` goes back to it."""

    question: str
    history: list[Turn]  # earlier turns of the conversation, masked, oldest first
    turn: Turn | None  # this question's turn, written by ``remember``
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
        self.pii = self.dictionary.pii_columns()
        self.privacy = privacy or load_pseudonymizer(settings.db_path, self.pii)
        self.schema_warnings: list[str] = []
        self.schema: SchemaInfo | None = None
        self.prompt_version = prompt_version(settings.dictionary_path)
        self.conversations = ConversationStore()
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
            history=state["history"],
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
        extra = self._derived_map(state["results"])
        decision = reply.decision.model_copy(update={"sql": self.privacy.unmask_sql(reply.decision.sql, extra=extra)})
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
            outcome = outcome.model_copy(update={"pii_columns": self._pii_columns(decision.sql, outcome)})
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

    def _remember(self, state: AgentState) -> dict[str, object]:
        """Append the finished turn to the thread's history, masked and without result rows."""
        response = state["response"]
        if response is None:  # unreachable by routing; guards against graph wiring mistakes
            raise RuntimeError("remember reached without a response")
        p = self.privacy
        sql = response.data.sql if response.data is not None else ""
        turn = Turn(
            question=p.mask(response.question),
            sql=p.mask(sql),
            answer=p.mask(response.answer),
            status=response.status,
        )
        return {"turn": turn}

    def _pii_columns(self, sql: str, result: QueryResult) -> list[int]:
        """Result columns carrying (pieces of) personal data, masked before the model reads them."""
        tables = self.schema.tables if self.schema is not None else []
        schema = {t.name: {c.name: c.declared_type or "TEXT" for c in t.columns} for t in tables}
        return sorted(pii_derived_columns(sql, schema, self.pii, width=len(result.columns)))

    def _derived_map(self, results: object) -> dict[str, str]:
        """Derived tokens of this turn's results back to their real values."""
        found: dict[str, str] = {}
        if isinstance(results, dict):
            for result in results.values():
                found.update(self.privacy.derived_map(result.rows, result.pii_columns))
        return found

    # ---------------------------------------------------------------- routing

    @staticmethod
    def _after_decide(state: AgentState) -> Literal["execute", "finalize", "refuse", "remember"]:
        if state["response"] is not None or state["decision"] is None:
            return "remember"
        return {"run_query": "execute", "final_answer": "finalize", "refuse": "refuse"}[  # type: ignore[return-value]
            state["decision"].action
        ]

    @staticmethod
    def _loop_or_end(state: AgentState) -> Literal["decide", "remember"]:
        return "remember" if state["response"] is not None else "decide"

    def _build(self) -> CompiledStateGraph:  # type: ignore[type-arg]
        graph = StateGraph(AgentState)
        graph.add_node("load_context", self._load_context)
        graph.add_node("decide", self._decide)
        graph.add_node("execute", self._execute)
        graph.add_node("finalize", self._finalize)
        graph.add_node("refuse", self._refuse)
        graph.add_node("remember", self._remember)
        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "decide")
        graph.add_conditional_edges("decide", self._after_decide)
        graph.add_conditional_edges("execute", self._loop_or_end)
        graph.add_conditional_edges("finalize", self._loop_or_end)
        graph.add_edge("refuse", "remember")
        graph.add_edge("remember", END)
        return graph.compile()

    # ------------------------------------------------------------------ entry

    def ask(self, question: str, *, thread_id: str | None = None, on_progress: Listener | None = None) -> AgentResponse:
        """Answer ``question``; ``on_progress`` receives each stage as it starts (see ``src/progress.py``).

        Questions with the same ``thread_id`` share a conversation: the model sees the last turns and
        can resolve follow-ups like "e no Chat?". ``None`` means a standalone question.
        """
        trace_id = uuid.uuid4().hex[:12]
        config: RunnableConfig = {"recursion_limit": 4 * self.limits.max_steps + 10}
        with self.conversations.hold(thread_id) if thread_id is not None else nullcontext():
            started = time.monotonic()
            initial: AgentState = {
                "question": question,
                "history": self.conversations.turns(thread_id) if thread_id is not None else [],
                "turn": None,
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
            with listening(on_progress), run_scope(RunInfo(trace_id, self.prompt_version, thread_id)):
                final = self._graph.invoke(initial, config)
            if thread_id is not None and final["turn"] is not None:
                self.conversations.append(thread_id, final["turn"])
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
        return self._unmasked(response, self._seen_tokens(question, final), self._derived_map(final.get("results")))

    def history(self, thread_id: str) -> list[Turn]:
        """The turns kept for ``thread_id`` (masked), oldest first; empty for an unknown thread."""
        return self.conversations.turns(thread_id)

    def _seen_tokens(self, question: str, final: dict[str, object]) -> set[str]:
        """Tokens the model was shown in this turn: the question, its query results and the history.

        Only these go back to real values in the answer, so a model steered by injected text cannot
        turn ``[email_1]|[email_2]|...`` into the real list (feedback 2026-11-14, 2.1).
        """
        p = self.privacy
        seen = p.tokens_in(p.mask(question))
        results = final.get("results")
        if isinstance(results, dict):
            for result in results.values():
                for row in p.mask_rows(result.rows, derived=result.pii_columns):
                    seen.update(t for v in row if isinstance(v, str) for t in p.tokens_in(v))
        history = final.get("history")
        if isinstance(history, list):
            for turn in history:  # earlier turns only: this one is in ``final["turn"]``
                seen |= p.tokens_in(f"{turn.question} {turn.sql} {turn.answer}")
        return seen

    def _unmasked(self, response: AgentResponse, seen: set[str], derived: dict[str, str]) -> AgentResponse:
        """What the user sees: no external content, and the turn's tokens turned back into real values.

        A derived token from an earlier turn stays a token: its value is not in this turn's results.
        """
        p = self.privacy

        def show(text: str) -> str:
            return p.unmask(strip_external(text), allowed=seen, extra=derived)

        return response.model_copy(
            update={
                "answer": show(response.answer),
                "assumptions": [show(a) for a in response.assumptions],
                "steps": [s.model_copy(update={"thought": show(s.thought)}) for s in response.steps],
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
