"""Streamlit interface: ``streamlit run app.py`` (or ``make run``).

Thin layer over ``Assistant.ask``. Presentation decisions live in ``src/ui/view.py`` and
``src/ui/query_graph.py`` (tested); copy lives in ``src/ui/texts.py``.

Layout is a conversation: a welcome message with the data coverage and suggestions, then each
question and its answer one below the other (answer → premise → result card → "how I got here").
Badges appear only for exceptions (refusal, failure, self-correction). Depth (white messages
with shadow, graphite user bubble, recessed cards) comes from ``src/ui/style.css`` painting the
keyed containers listed in ``src/ui/style.py``.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import streamlit as st

from src.agent.graph import Assistant
from src.agent.models import AgentResponse, Step
from src.config import Price, Settings, load_env_file
from src.db.tools import QueryResult, discover_schema
from src.llm import LLMError, get_llm
from src.observability import Tracer
from src.progress import Progress
from src.semantic.dictionary import load_dictionary
from src.ui import data_graph, query_graph, style, texts, view
from src.ui.progress_log import ProgressLog

ASSISTANT_AVATAR = ":material/query_stats:"
USER_BUBBLE_INDENT_PX = 96

st.set_page_config(
    page_title=f"{texts.APP_TITLE} · {texts.SUBTITLE}",
    page_icon=":material/query_stats:",
    initial_sidebar_state="collapsed",
)


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    tracer: Tracer
    assistant: Assistant | None
    error: str | None
    coverage: tuple[str, str] | None
    columns: dict[str, frozenset[str]]  # table -> column names, to resolve the query graph


@st.cache_resource
def runtime() -> Runtime:
    load_env_file()
    settings = Settings.from_env()
    tracer = Tracer(settings.trace_path)
    try:
        assistant: Assistant | None = Assistant(get_llm(), settings, tracer=tracer)
        error = None
    except LLMError as err:
        assistant, error = None, str(err)
    unreliable = load_dictionary(settings.dictionary_path).unreliable_columns()
    schema = discover_schema(
        settings.db_path, max_categorical=settings.max_categorical_values, exclude_from_reference=unreliable
    )
    columns = {t.name: frozenset(c.name for c in t.columns) for t in schema.tables}
    return Runtime(settings, tracer, assistant, error, view.coverage(schema, unreliable), columns)


def new_conversation() -> None:
    """Forget the conversation: a new thread for the agent's memory and an empty screen."""
    st.session_state.thread_id = uuid.uuid4().hex
    st.session_state.history = []


def render_sidebar(rt: Runtime) -> None:
    with st.sidebar:
        st.button(
            texts.NEW_CONVERSATION,
            help=texts.NEW_CONVERSATION_HELP,
            icon=":material/add_comment:",
            on_click=new_conversation,
            disabled=rt.assistant is None,
        )
        st.markdown(f"### {texts.SIDEBAR_ABOUT}")
        st.caption(texts.TAGLINE)
        if rt.assistant is None:
            return
        unreliable = sorted(rt.assistant.dictionary.unreliable_columns())
        if unreliable:
            st.caption(texts.SIDEBAR_UNRELIABLE.format(columns=", ".join(f"`{c}`" for c in unreliable)))
        if rt.assistant.schema_warnings:
            with st.expander(texts.SIDEBAR_SCHEMA_WARNINGS):
                for warning in rt.assistant.schema_warnings:
                    st.caption(warning)
        st.markdown(f"**{texts.SIDEBAR_MODEL}**")
        st.caption(os.getenv("GEMINI_MODELS") or rt.assistant.llm.model_name)
        st.caption(texts.SIDEBAR_CACHE_MODE.get(os.getenv("LLM_CACHE", "on").lower(), ""))


def download_button(data: QueryResult, key: str) -> None:
    st.download_button(
        texts.DOWNLOAD_CSV,
        data=view.to_csv(data),
        file_name=f"indaga-{key[:8]}.csv",
        mime="text/csv",
        key=f"csv-{key}",
        icon=":material/download:",
    )


def render_result(response: AgentResponse) -> None:
    """Titled card: chart or table, then the Gráfico/Tabela switch and the CSV download."""
    data = response.data
    if data is None:
        return
    if data.row_count == 0:
        st.caption(texts.NO_ROWS)
        return
    chart = view.drawable(response.chart, data)
    key = response.trace_id or str(id(response))
    with st.container(border=True, key=style.result_key(key)):
        if chart.type == "metric":
            st.metric(label=view.result_title(chart, data), value=view.metric_value(chart, data))
            with st.container(horizontal=True, horizontal_alignment="right"):
                download_button(data, key)
            return
        default = texts.VIEW_CHART if chart.type in ("bar", "line") else texts.VIEW_TABLE
        if chart.type == "table":
            chart = view.drawable(view.infer_chart(data), data)
        st.markdown(f"**{view.result_title(chart, data)}**")
        st.caption(view.row_caption(data))
        body = st.container()
        with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
            mode = texts.VIEW_TABLE
            if chart.type in ("bar", "line"):
                picked = st.segmented_control(
                    "Visualização",
                    [texts.VIEW_CHART, texts.VIEW_TABLE],
                    default=default,
                    key=f"view-{key}",
                    label_visibility="collapsed",
                )
                mode = picked or default
            download_button(data, key)
        spec = view.build_chart(chart, data) if mode == texts.VIEW_CHART else None
        with body:
            if spec is not None:
                st.altair_chart(spec, theme=None, width="stretch")
            else:
                st.dataframe(view.display_records(data), hide_index=True, width="stretch")


def render_step(rt: Runtime, step: Step) -> None:
    title = f"**{step.index}. {texts.STEP_LABEL.get(step.kind, step.kind)}**"
    if step.row_count is not None:
        rows = "linha" if step.row_count == 1 else "linhas"
        title += f" · {step.row_count} {rows} · {step.elapsed_ms:.0f} ms"
    st.markdown(title)
    if step.thought:
        st.caption(step.thought)
    if step.sql and step.kind == "query":
        graph = query_graph.extract(step.sql, rt.columns)
        if graph is not None:
            st.graphviz_chart(query_graph.to_dot(graph, step.row_count))
            st.caption(texts.GRAPH_CAPTION)
    if step.sql:
        st.code(step.sql, language="sql", wrap_lines=True)
    if step.error:
        st.markdown(f":red[{step.error}]")


def render_reasoning(rt: Runtime, response: AgentResponse) -> None:
    queries = sum(1 for s in response.steps if s.kind in ("query", "query_error"))
    parts = [texts.REASONING]
    if queries:
        parts.append(f"{queries} {'consulta' if queries == 1 else 'consultas'}")
    parts.append(f"{response.llm_calls} {'chamada' if response.llm_calls == 1 else 'chamadas'} ao modelo")
    parts.append("< 1 s" if response.latency_ms < 1000 else f"{response.latency_ms / 1000:.0f} s")
    with st.expander(" · ".join(parts), icon=":material/account_tree:"):
        for step in response.steps:
            render_step(rt, step)
        st.caption(
            f"{response.model or 'sem modelo'} · {response.cache_hits} de {response.llm_calls} chamadas do cache · "
            f"{response.usage.input_tokens}+{response.usage.output_tokens} tokens · trace `{response.trace_id}`"
        )


def render_response(response: AgentResponse, rt: Runtime) -> None:
    badges = []
    if response.status != "answered":
        badges.append(f":red-badge[{texts.STATUS_LABEL[response.status]}]")
    if response.corrections:
        badges.append(f":orange-badge[Autocorrigida: {response.corrections}]")
    if badges:
        st.markdown(" ".join(badges))
    st.markdown(response.answer)
    for assumption in response.assumptions:
        st.markdown(f":orange-badge[{texts.ASSUMPTION}] {assumption}")
    if rt.assistant is not None:
        for note in view.quality_notes(response, rt.assistant.dictionary):
            st.caption(f":orange[⚠] {note}")
    render_result(response)
    render_reasoning(rt, response)


def render_welcome(rt: Runtime, asked: set[str]) -> str | None:
    """Assistant's first message: scope, data coverage and the five suggested questions."""
    pending = None
    with st.container(key=style.WELCOME), st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
        if rt.error:
            st.error(texts.MISSING_KEY.format(error=rt.error), icon=":material/key_off:")
        st.markdown(texts.WELCOME)
        if rt.coverage:
            start, end = (view.br_date(d) for d in rt.coverage)
            st.markdown(texts.WELCOME_COVERAGE.format(start=start, end=end))
        if rt.assistant is None:
            return None
        st.markdown(texts.WELCOME_SUGGESTIONS)
        # One per line, sized to the label: in two columns the labels did not fit and were cut with "…".
        buttons = st.container(key=style.SUGGESTIONS)
        suggestions = zip(texts.SUGGESTED_QUESTIONS, texts.SUGGESTION_LABELS, strict=True)
        for i, (question, (kind, short)) in enumerate(suggestions):
            icon = ":material/check:" if question in asked else ":material/arrow_outward:"
            if buttons.button(f"**{kind}** · {short}", key=f"suggest-{i}", icon=icon, help=question, width="content"):
                pending = question
    return pending


def user_bubble(question: str, turn: int | str, *, scroll_here: bool = False) -> None:
    """The user's question as a right-aligned bubble (alignment from the API, paint from style.css).

    The fixed space on the left keeps long questions from spanning the full width, like a chat.
    """
    with st.container(horizontal=True, horizontal_alignment="right"):
        if scroll_here:  # in this row it adds no vertical gap; static script, no user data
            st.html(style.scroll_below_banner_js(style.user_key(turn)), unsafe_allow_javascript=True, width="content")
        st.space(USER_BUBBLE_INDENT_PX)
        with st.container(key=style.user_key(turn), width="content"):
            st.markdown(question)


def ask(rt: Runtime, question: str) -> None:
    if rt.assistant is None:  # the input is disabled in this case
        return
    user_bubble(question, "pending", scroll_here=True)
    with (
        st.container(key=style.bot_key("pending")),
        st.chat_message("assistant", avatar=ASSISTANT_AVATAR),
        st.status(texts.PROGRESS_START, expanded=True) as box,
    ):
        # Each Gemini call takes seconds: show the stage in progress and the ones already done.
        st.caption(texts.PROGRESS_HINT)
        checklist = st.empty()
        log = ProgressLog()

        def show(event: Progress) -> None:
            log.apply(event)
            box.update(label=log.label, expanded=True)  # without expanded, Streamlit 1.67 collapses it
            checklist.markdown("  \n".join(f":material/check: {line}" for line in log.done))

        response = rt.assistant.ask(question, thread_id=st.session_state.thread_id, on_progress=show)
        # Collapse before the rerun: the answer's "how I got here" expander takes this element's place
        # in the page and would otherwise inherit its open state.
        box.update(state="complete", expanded=False)
    st.session_state.history.append(response)
    st.session_state.show_ask_tab = True  # applied before the tabs are created on the rerun
    st.rerun()


def render_ask_tab(rt: Runtime) -> None:
    history: list[AgentResponse] = st.session_state.history
    pending = render_welcome(rt, {r.question for r in history})
    for turn, response in enumerate(history):
        user_bubble(response.question, turn)
        with st.container(key=style.bot_key(turn)), st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
            render_response(response, rt)
    typed = st.chat_input(texts.INPUT_PLACEHOLDER, disabled=rt.assistant is None)
    question = typed or pending
    if question:
        ask(rt, question)


@st.cache_data(show_spinner=False)
def graph_memberships(db_path: str, modified: float, keys: tuple[str, ...]) -> data_graph.Memberships:
    """Cached per database file and modification time, like the schema discovery."""
    dims = [d for d in data_graph.DIMENSIONS if d.key in keys]
    return data_graph.load_memberships(Path(db_path), dims)


@st.cache_data(show_spinner=False)
def graph_page(db_path: str, modified: float, keys: tuple[str, ...], min_clients: int) -> tuple[str, int, int]:
    dims = [d for d in data_graph.DIMENSIONS if d.key in keys]
    graph = data_graph.build(graph_memberships(db_path, modified, keys), dims, min_clients)
    return data_graph.to_html(graph, dims), len(graph.nodes), len(graph.edges)


def render_graph_tab(rt: Runtime) -> None:
    st.caption(texts.GRAPH_INTRO)
    dims = data_graph.available(rt.columns)
    by_label = {d.label: d for d in dims}
    with st.container(horizontal=True, vertical_alignment="bottom"):
        picked = st.multiselect(
            texts.GRAPH_DIMENSIONS,
            list(by_label),
            default=[d.label for d in dims if d.key in data_graph.DEFAULT_SELECTION],
            key="graph-dims",
        )
        min_clients = st.slider(texts.GRAPH_MIN_CLIENTS, 1, 10, 3, key="graph-min", width=260)
    chosen = [by_label[label] for label in picked]
    if len(chosen) < 2:
        st.caption(texts.GRAPH_PICK_TWO)
        return
    db = rt.settings.db_path
    keys = tuple(d.key for d in chosen)
    page, nodes, edges = graph_page(str(db), db.stat().st_mtime, keys, min_clients)
    st.caption(texts.GRAPH_SUMMARY.format(nodes=nodes, edges=edges))
    if edges == 0:
        st.caption(texts.GRAPH_EMPTY)
        return
    st.iframe(page, height=data_graph.HEIGHT_PX + 50)
    with st.expander(texts.GRAPH_TABLE, icon=":material/table_rows:"):
        graph = data_graph.build(graph_memberships(str(db), db.stat().st_mtime, keys), chosen, min_clients)
        st.dataframe(data_graph.strongest(graph), hide_index=True, width="stretch")


METRICS_PER_ROW = 5


def render_trace_metrics(traces: list[dict[str, Any]], price: Price | None) -> None:
    summary = view.trace_summary(traces, price)
    st.caption(texts.TRACES_METRICS_CAPTION.format(n=len(traces)))
    for start in range(0, len(summary), METRICS_PER_ROW):
        row = summary[start : start + METRICS_PER_ROW]
        for column, (label, value) in zip(st.columns(METRICS_PER_ROW), row, strict=False):
            column.metric(label, value, help=texts.METRIC_HELP.get(label))


def render_traces_tab(rt: Runtime) -> None:
    st.caption(texts.TRACES_CAPTION.format(path=rt.settings.trace_path.name))
    traces = rt.tracer.tail(30)
    if not traces:
        st.caption(texts.TRACES_EMPTY)
        return
    render_trace_metrics(traces, rt.settings.price)
    st.dataframe(view.trace_table(traces), hide_index=True, width="stretch")
    with st.expander("Último registro completo (JSON)"):
        st.json(traces[-1])


def main() -> None:
    rt = runtime()
    st.html(style.CSS_PATH)
    st.session_state.setdefault("history", [])
    if "thread_id" not in st.session_state:  # one conversation per browser session, kept by the agent
        st.session_state.thread_id = uuid.uuid4().hex
    render_sidebar(rt)
    with st.container(key=style.MASTHEAD):
        st.html(style.banner_html())
    with st.container(key=style.HEADLINE):
        st.title(f"{texts.APP_TITLE} :small[· {texts.SUBTITLE}]", anchor=False)
    if st.session_state.pop("show_ask_tab", False):
        st.session_state["main_tab"] = texts.TAB_ASK
    tab_ask, tab_graph, tab_traces = st.tabs(
        [texts.TAB_ASK, texts.TAB_GRAPH, texts.TAB_TRACES], default=texts.TAB_ASK, key="main_tab", on_change="rerun"
    )
    with tab_ask:
        render_ask_tab(rt)
    if tab_graph.open:  # the graph page is ~700 KB; send it only while the tab is visible
        with tab_graph:
            render_graph_tab(rt)
    with tab_traces:
        render_traces_tab(rt)


main()
