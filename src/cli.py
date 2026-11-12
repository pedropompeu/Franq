"""Ask a question from the terminal: ``python -m src.cli "sua pergunta"``."""

from __future__ import annotations

import argparse
import sys

from src.agent.graph import Assistant
from src.config import Settings, load_env_file
from src.llm import LLMError, get_llm
from src.observability import Tracer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assistente Virtual de Dados")
    parser.add_argument("question", help="pergunta em linguagem natural")
    args = parser.parse_args(argv)

    load_env_file()
    settings = Settings.from_env()
    try:
        llm = get_llm()
    except LLMError as err:
        print(f"Erro: {err}", file=sys.stderr)
        return 2

    response = Assistant(llm, settings, tracer=Tracer(settings.trace_path)).ask(args.question)
    print(f"[{response.status}] {response.answer}")
    for assumption in response.assumptions:
        print(f"  premissa: {assumption}")
    for step in response.steps:
        print(f"\n--- passo {step.index} · {step.kind} · {step.thought}")
        if step.sql:
            print(step.sql)
        if step.error:
            print(f"erro: {step.error}")
    if response.data:
        print(f"\nresultado ({response.data.row_count} linhas, gráfico: {response.chart.type}):")
        print(" | ".join(response.data.columns))
        for row in response.data.rows[:20]:
            print(" | ".join(str(v) for v in row))
    print(
        f"\n{response.llm_calls} chamadas ao LLM · {response.corrections} correções · "
        f"{response.usage.input_tokens}+{response.usage.output_tokens} tokens · {response.latency_ms:.0f} ms · "
        f"trace {response.trace_id}"
    )
    return 0 if response.status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
