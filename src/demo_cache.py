"""Warm, or check, the LLM cache for the demo conversations, in the order the screen asks them.

The screen sends every question with its conversation's ``thread_id`` and the prompt carries the
previous turns (``src/agent/prompts.py``), so the cache key of a follow-up depends on what was asked
before it. ``src.cli`` answers one question per process, without memory, and cannot reproduce that:
this module runs each demo conversation in one process, one thread per conversation.

    LLM_CACHE=on make demo-cache       # warm: only questions missing from the cache spend quota
    LLM_CACHE=replay make demo-cache   # check: every demo question must answer from the cache
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Callable, Sequence

from src.agent.graph import Assistant
from src.config import Settings, load_env_file
from src.llm import LLMError, get_llm
from src.observability import Tracer
from src.ui import texts

DELETE_REQUEST = "Apague todas as reclamações resolvidas"
FOLLOW_UP = "E só no Chat?"

Conversation = tuple[str, ...]


def demo_conversations() -> tuple[Conversation, ...]:
    """The demo conversations, each as the screen asks it (a new conversation per tuple)."""
    ranking, whatsapp, average, by_channel, trend = texts.SUGGESTED_QUESTIONS
    return (
        (trend, DELETE_REQUEST),  # the "Tendência" suggestion, then the write request in the same conversation
        (by_channel, FOLLOW_UP),  # conversation memory: a follow-up that only makes sense after P4
        (ranking,),
        (whatsapp,),
        (average,),
        (DELETE_REQUEST,),  # the write request on a fresh conversation
    )


def run(assistant: Assistant, conversations: Sequence[Conversation], out: Callable[[str], None] = print) -> int:
    """Ask every question in order, one thread per conversation; return how many failed."""
    failures = 0
    for number, conversation in enumerate(conversations, start=1):
        thread = uuid.uuid4().hex
        for question in conversation:
            response = assistant.ask(question, thread_id=thread)
            failures += response.status == "failed"
            cache = f"{response.cache_hits}/{response.llm_calls} do cache"
            out(f"[{response.status}] conversa {number} · {cache} · {question}")
    return failures


def main() -> int:
    load_env_file()
    settings = Settings.from_env()
    try:
        llm = get_llm()
    except LLMError as err:
        print(f"Erro: {err}", file=sys.stderr)
        return 2
    failures = run(Assistant(llm, settings, tracer=Tracer(settings.trace_path)), demo_conversations())
    print(f"\n{failures} pergunta(s) falharam." if failures else "\nTodas as perguntas da demo responderam.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
