"""Optional LangFuse tracing and dataset runs, fed only with masked data.

Without ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` nothing here runs and ``langfuse`` is
never imported. With them:

* :class:`TracedLLM` wraps the provider chain and records one *generation* per model decision.
  It sits below the graph, so it only sees the messages the graph already masked
  (``[nome_1]`` instead of a real name). The LangFuse client never goes into the graph
  ``invoke`` config, whose state holds the real rows.
* :class:`LangfuseDatasetSink` turns an evaluation run into a LangFuse dataset run: one item per
  case (masked question, masked reference SQL) and the scores ``acerto``, ``correcoes`` and
  ``latencia_ms``.

langfuse 5.2 calls used: ``Langfuse(public_key, secret_key, base_url)``,
``Langfuse.create_trace_id(seed)``, ``propagate_attributes(session_id, tags, version, trace_name)``,
``start_observation(trace_context, name, as_type="generation", input, output, model, usage_details,
metadata)`` + ``end()``, ``create_dataset``, ``create_dataset_item(id=...)`` (upsert),
``run_experiment(name, run_name, data, task, evaluators)`` and ``flush()``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.messages import BaseMessage

from src.agent.models import TokenUsage
from src.llm import LLMReply, StructuredLLM
from src.run_context import RunInfo, current_run

if TYPE_CHECKING:
    from src.evaluation import CaseRun

logger = logging.getLogger(__name__)

DEFAULT_DATASET = "indaga-eval"
TRACE_NAME = "indaga.ask"


class GenerationSink(Protocol):
    def record(
        self,
        *,
        name: str,
        input: list[dict[str, str]],
        output: dict[str, Any],
        model: str,
        usage: TokenUsage,
        cached: bool,
        run: RunInfo | None,
    ) -> None: ...


class TracedLLM:
    """Records every decision in a :class:`GenerationSink`; a sink failure never reaches the user."""

    def __init__(self, inner: StructuredLLM, sink: GenerationSink) -> None:
        self.inner, self.sink = inner, sink

    @property
    def model_name(self) -> str:
        return self.inner.model_name

    def decide(self, messages: Sequence[BaseMessage]) -> LLMReply:
        reply = self.inner.decide(messages)
        try:
            self.sink.record(
                name="decide",
                input=[{"role": m.type, "content": str(m.content)} for m in messages],
                output=reply.decision.model_dump(),
                model=reply.model,
                usage=reply.usage,
                cached=reply.cached,
                run=current_run(),
            )
        except Exception as err:
            logger.warning("langfuse: generation not recorded (%s)", type(err).__name__)
        return reply


def _client_from_env() -> Any | None:
    public, secret = os.getenv("LANGFUSE_PUBLIC_KEY", ""), os.getenv("LANGFUSE_SECRET_KEY", "")
    if not public or not secret:
        return None
    from langfuse import Langfuse

    return Langfuse(public_key=public, secret_key=secret, base_url=os.getenv("LANGFUSE_HOST") or None)


class LangfuseSink:
    """One generation per decision, grouped by our ``trace_id``; session = conversation thread."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def record(
        self,
        *,
        name: str,
        input: list[dict[str, str]],
        output: dict[str, Any],
        model: str,
        usage: TokenUsage,
        cached: bool,
        run: RunInfo | None,
    ) -> None:
        from langfuse import propagate_attributes

        attributes: dict[str, Any] = {"trace_name": TRACE_NAME}
        trace_context: dict[str, str] | None = None
        metadata: dict[str, Any] = {"cached": cached}
        if run is not None:
            trace_context = {"trace_id": self.client.create_trace_id(seed=run.trace_id)}
            attributes |= {"tags": [run.prompt_version], "version": run.prompt_version}
            if run.thread_id:
                attributes["session_id"] = run.thread_id
            metadata["indaga_trace_id"] = run.trace_id
        with propagate_attributes(**attributes):
            generation = self.client.start_observation(
                trace_context=trace_context,
                name=name,
                as_type="generation",
                input=input,
                output=output,
                model=model,
                usage_details={"input": usage.input_tokens, "output": usage.output_tokens},
                metadata=metadata,
            )
            generation.end()


def langfuse_sink_from_env() -> GenerationSink | None:
    """``None`` unless both LangFuse keys are set; ``langfuse`` is imported only here."""
    client = _client_from_env()
    return LangfuseSink(client) if client is not None else None


# ---------------------------------------------------------------- dataset runs


class DatasetSink(Protocol):
    def record_case(self, run: CaseRun, *, run_name: str) -> None: ...


class LangfuseDatasetSink:
    """Each evaluated case becomes a dataset item (upsert by case id) plus a scored experiment item."""

    def __init__(self, client: Any, dataset: str = DEFAULT_DATASET, *, evaluation_cls: Any = None) -> None:
        if evaluation_cls is None:
            from langfuse import Evaluation

            evaluation_cls = Evaluation
        self.client, self.dataset, self.evaluation_cls = client, dataset, evaluation_cls
        self._created = False

    def record_case(self, run: CaseRun, *, run_name: str) -> None:
        if not self._created:
            self.client.create_dataset(name=self.dataset, description="Perguntas da avaliação do Indaga (mascaradas)")
            self._created = True
        case = run.case
        item = self.client.create_dataset_item(
            dataset_name=self.dataset,
            id=f"{self.dataset}-{case.id}",
            input=case.question,  # questions.yaml is already masked
            expected_output=case.reference,
            metadata={"case_id": case.id, "kind": case.kind},
        )
        output = {
            "status": run.status,
            "sql": run.sql,  # masked by run_case
            "passed": run.passed,
            "failed_checks": run.failed_checks,
            "corrections": run.corrections,
            "latency_ms": run.latency_ms,
            "trace_id": run.trace_id,
            "model": run.model,
        }
        make = self.evaluation_cls

        def task(*, item: Any, **_kwargs: Any) -> dict[str, Any]:
            return output

        def scores(*, output: dict[str, Any], **_kwargs: Any) -> list[Any]:
            return [
                make(name="acerto", value=1 if output["passed"] else 0, comment=", ".join(output["failed_checks"])),
                make(name="correcoes", value=output["corrections"]),
                make(name="latencia_ms", value=output["latency_ms"]),
            ]

        self.client.run_experiment(name=self.dataset, run_name=run_name, data=[item], task=task, evaluators=[scores])
        self.client.flush()


def langfuse_dataset_sink(dataset: str = DEFAULT_DATASET) -> DatasetSink:
    client = _client_from_env()
    if client is None:
        raise RuntimeError("--langfuse exige LANGFUSE_PUBLIC_KEY e LANGFUSE_SECRET_KEY no .env.")
    return LangfuseDatasetSink(client, dataset)
