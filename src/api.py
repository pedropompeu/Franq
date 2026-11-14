"""HTTP entry point: the same ``Assistant`` as the Streamlit app, behind FastAPI.

Routes:
- ``GET /health``: model and prompt version (never needs the key, for load balancers).
- ``POST /conversations``: opens a conversation → ``{"thread_id"}``.
- ``POST /ask``: ``{"question", "thread_id"?}`` → ``AgentResponse`` as JSON.
- ``POST /ask/stream``: the same, as Server-Sent Events: one ``progress`` event per stage
  (``src/progress.py``), then one ``answer`` event, or one ``error`` event.

Access (feedback 2026-11-14, 2.5 and 2.6):

- ``API_KEY`` (one or more, comma separated) makes every route but ``/health`` require the
  ``X-API-Key`` header. Without it the API serves only loopback and private addresses (the laptop,
  compose's bridge); a request from a public address gets 403. Behind a reverse proxy every caller
  looks local, so set ``API_KEY`` there.
- ``thread_id`` values are issued by the server (``POST /conversations``, 128 random bits) and
  belong to the key that opened them: an id that was not issued to the caller's key is a 404, so a
  guessed or leaked id from another key opens nothing.
- ``API_RATE_PER_MIN`` questions per minute per key and address (default 10); beyond it, 429 with
  ``Retry-After``.

Responses carry real values (names, e-mails), exactly like the screen; only the LLM and the
traces see them masked. Provider errors reach the client as a generic message (``src/llm.py``).

Run: ``uvicorn src.api:app`` (``make run-api``). Importing this module builds nothing: the
assistant is assembled when the server starts.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
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
LOCAL_ONLY = "Sem API_KEY a API só atende esta máquina ou a rede local. Defina API_KEY no .env."
BAD_KEY = "Cabeçalho X-API-Key ausente ou inválido."
UNKNOWN_THREAD = "Conversa desconhecida ou expirada. Abra outra em POST /conversations."
TOO_MANY = "Muitas perguntas em pouco tempo. Tente de novo em instantes."
RATE_PER_MIN = 10


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500, description="Pergunta em português.")
    thread_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
        description="Id devolvido por POST /conversations; sem ele, a pergunta é avulsa.",
    )


class Conversation(BaseModel):
    thread_id: str


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


def _keys(raw: str | None) -> tuple[str, ...]:
    return tuple(k.strip() for k in (raw or "").split(",") if k.strip())


def _is_local(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private


class RateWindow:
    """Sliding one-minute window per client; ``per_min <= 0`` disables it."""

    def __init__(self, per_min: int, *, clock: Callable[[], float] = time.monotonic, max_clients: int = 10_000) -> None:
        self.per_min = per_min
        self._clock = clock
        self._max_clients = max_clients
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def wait(self, client: str) -> float:
        """0 when ``client`` may go now (the hit is counted), else seconds until it may."""
        if self.per_min <= 0:
            return 0.0
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(client, deque())
            self._hits.move_to_end(client)
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= self.per_min:
                return hits[0] + 60 - now
            hits.append(now)
            while len(self._hits) > self._max_clients:
                self._hits.popitem(last=False)
            return 0.0


def _caller(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> str:
    """Authenticate and rate-limit; returns the principal that namespaces this caller's conversations."""
    host = request.client.host if request.client else ""
    keys: tuple[str, ...] = request.app.state.api_keys
    if keys:
        presented = (x_api_key or "").encode()
        matches = [k for k in keys if hmac.compare_digest(presented, k.encode())]  # no short-circuit
        if not matches:
            raise HTTPException(401, detail=BAD_KEY)
        principal = hashlib.sha256(matches[0].encode()).hexdigest()[:16]
    elif _is_local(host):
        principal = "local"
    else:
        raise HTTPException(403, detail=LOCAL_ONLY)
    window: RateWindow = request.app.state.rate
    if (wait := window.wait(f"{principal}|{host}")) > 0:
        raise HTTPException(429, detail=TOO_MANY, headers={"Retry-After": str(max(1, math.ceil(wait)))})
    return principal


def _thread(agent: Assistant, principal: str, thread_id: str | None) -> str | None:
    """The store key of a server-issued ``thread_id``; 404 when it was not issued to ``principal``."""
    if thread_id is None:
        return None
    key = f"{principal}:{thread_id}"
    if key not in agent.conversations:
        raise HTTPException(404, detail=UNKNOWN_THREAD)
    return key


# Module level on purpose: with ``from __future__ import annotations`` FastAPI resolves route
# annotations by name in this module's globals, so an alias local to ``create_app`` would be lost.
AssistantDep = Annotated[Assistant, Depends(_current_assistant)]
CallerDep = Annotated[str, Depends(_caller)]


def _rate_from_env() -> int:
    raw = os.getenv("API_RATE_PER_MIN", "").strip()
    try:
        return int(raw) if raw else RATE_PER_MIN
    except ValueError:
        log.warning("API_RATE_PER_MIN inválido (%r); usando %d", raw, RATE_PER_MIN)
        return RATE_PER_MIN


def create_app(
    assistant: Assistant | None = None, *, api_key: str | None = None, rate_per_min: int | None = None
) -> FastAPI:
    """Without ``assistant``, build it from the environment (``.env`` included) when the server starts.

    An injected assistant (tests, embedding) uses only the arguments; one built from the
    environment also reads ``API_KEY`` and ``API_RATE_PER_MIN``.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.assistant is None and app.state.error is None:
            load_env_file()
            app.state.assistant, app.state.error = _build_from_env()
            app.state.api_keys = _keys(api_key or os.getenv("API_KEY"))
            app.state.rate = RateWindow(rate_per_min if rate_per_min is not None else _rate_from_env())
        yield

    app = FastAPI(title="Indaga", summary="Perguntas de negócio em português sobre o banco.", lifespan=lifespan)
    app.state.assistant = assistant
    app.state.error = None
    app.state.api_keys = _keys(api_key)
    app.state.rate = RateWindow(rate_per_min if rate_per_min is not None else RATE_PER_MIN)

    @app.get("/health")
    def health(agent: AssistantDep) -> Health:
        return Health(status="ok", model=agent.llm.model_name, prompt_version=agent.prompt_version)

    @app.post("/conversations", status_code=201)
    def open_conversation(agent: AssistantDep, principal: CallerDep) -> Conversation:
        thread_id = secrets.token_urlsafe(16)
        agent.conversations.open(f"{principal}:{thread_id}")
        return Conversation(thread_id=thread_id)

    @app.post("/ask")
    def ask(body: AskRequest, agent: AssistantDep, principal: CallerDep) -> AgentResponse:
        thread = _thread(agent, principal, body.thread_id)
        return agent.ask(body.question, thread_id=thread)  # sync: FastAPI runs it in a worker thread

    @app.post("/ask/stream")
    async def ask_stream(body: AskRequest, agent: AssistantDep, principal: CallerDep) -> StreamingResponse:
        thread = _thread(agent, principal, body.thread_id)
        return StreamingResponse(
            _stream(agent, body.question, thread),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _stream(agent: Assistant, question: str, thread_id: str | None) -> AsyncIterator[str]:
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
            response = agent.ask(question, thread_id=thread_id, on_progress=on_progress)
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
