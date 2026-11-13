"""HTTP entry point: the same ``Assistant`` as the Streamlit app, behind FastAPI.

Routes:
- ``GET /health``: model and prompt version (never needs the key, for load balancers).
- ``POST /ask``: ``{"question", "thread_id"?}`` → ``AgentResponse`` as JSON.
- ``POST /ask/stream``: the same, as Server-Sent Events: one ``progress`` event per stage
  (``src/progress.py``), then one ``answer`` event, or one ``error`` event.

The same ``thread_id`` keeps a conversation (see ``Assistant.ask``). With ``API_KEY`` set, every
route but ``/health`` requires the ``X-API-Key`` header. Responses carry real values (names,
e-mails), exactly like the screen; only the LLM and the traces see them masked.

Run: ``uvicorn src.api:app`` (``make run-api``). Importing this module builds nothing: the
assistant is assembled when the server starts.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import anyio.to_thread
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.agent.graph import Assistant
from src.agent.models import AgentResponse
from src.config import Settings, load_env_file
from src.llm import LLMError, get_llm
from src.observability import Tracer
from src.progress import Progress

log = logging.getLogger(__name__)

STREAM_ERROR = "Erro interno ao responder. Tente de novo; se persistir, veja os logs do servidor."


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500, description="Pergunta em português.")
    thread_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
        description="Mesma conversa entre chamadas; sem ele, a pergunta é avulsa.",
    )


class Health(BaseModel):
    status: str
    model: str
    prompt_version: str


def _build_from_env() -> tuple[Assistant | None, str | None]:
    settings = Settings.from_env()
    try:
        return Assistant(get_llm(), settings, tracer=Tracer(settings.trace_path)), None
    except LLMError as err:
        return None, str(err)


def _current_assistant(request: Request) -> Assistant:
    found: Assistant | None = request.app.state.assistant
    if found is None:
        raise HTTPException(503, detail=request.app.state.error or "O assistente ainda não foi iniciado.")
    return found


def _check_key(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected: str | None = request.app.state.api_key
    if expected and not hmac.compare_digest((x_api_key or "").encode(), expected.encode()):
        raise HTTPException(401, detail="Cabeçalho X-API-Key ausente ou inválido.")


# Module level on purpose: with ``from __future__ import annotations`` FastAPI resolves route
# annotations by name in this module's globals, so an alias local to ``create_app`` would be lost.
AssistantDep = Annotated[Assistant, Depends(_current_assistant)]


def create_app(assistant: Assistant | None = None, *, api_key: str | None = None) -> FastAPI:
    """Without ``assistant``, build it from the environment (``.env`` included) when the server starts.

    An injected assistant (tests, embedding) uses only ``api_key``; one built from the environment
    reads ``API_KEY`` too.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.assistant is None and app.state.error is None:
            load_env_file()
            app.state.assistant, app.state.error = _build_from_env()
            app.state.api_key = api_key or os.getenv("API_KEY") or None
        yield

    app = FastAPI(title="Indaga", summary="Perguntas de negócio em português sobre o banco.", lifespan=lifespan)
    app.state.assistant = assistant
    app.state.error = None
    app.state.api_key = api_key

    protected = [Depends(_check_key)]

    @app.get("/health")
    def health(agent: AssistantDep) -> Health:
        return Health(status="ok", model=agent.llm.model_name, prompt_version=agent.prompt_version)

    @app.post("/ask", dependencies=protected)
    def ask(body: AskRequest, agent: AssistantDep) -> AgentResponse:
        return agent.ask(body.question, thread_id=body.thread_id)  # sync: FastAPI runs it in a worker thread

    @app.post("/ask/stream", dependencies=protected)
    async def ask_stream(body: AskRequest, agent: AssistantDep) -> StreamingResponse:
        return StreamingResponse(
            _stream(agent, body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _stream(agent: Assistant, body: AskRequest) -> AsyncIterator[str]:
    """Run ``ask`` in a worker thread and relay its progress as it happens.

    The listener is called from that thread, so it hands each event to the event loop with
    ``call_soon_threadsafe``. ``None`` marks the end of the stream.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def push(item: str | None) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def on_progress(event: Progress) -> None:
        push(_sse("progress", dataclasses.asdict(event)))

    def run() -> None:
        try:
            response = agent.ask(body.question, thread_id=body.thread_id, on_progress=on_progress)
            push(_sse("answer", response.model_dump(mode="json")))
        except Exception:  # a bug, not an LLM failure (ask turns those into a "failed" answer)
            log.exception("ask failed in /ask/stream")
            push(_sse("error", {"detail": STREAM_ERROR}))
        finally:
            push(None)

    worker = asyncio.ensure_future(anyio.to_thread.run_sync(run))
    while (item := await queue.get()) is not None:
        yield item
    await worker


app = create_app()
