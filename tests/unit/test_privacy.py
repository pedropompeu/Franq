"""Personal data never reaches the LLM, the cache or the traces; the user still sees real values."""

from __future__ import annotations

import dataclasses
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from src.agent.graph import Assistant
from src.agent.prompts import build_messages
from src.config import Settings
from src.observability import Tracer
from src.privacy import PROMPT_NOTE, Pseudonymizer, load_pseudonymizer
from tests.unit.test_agent import FakeLLM, answer, query

PEOPLE = Pseudonymizer.from_rows(
    {
        "nome": ["Ana Lima", "João D'Ávila", "Bo"],
        "email": ["ana@example.com", "joao@example.net", "bo@example.org"],
    }
)


def first_client(db_path: Path) -> tuple[str, str]:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as con:
        nome, email = con.execute("SELECT nome, email FROM clientes ORDER BY rowid LIMIT 1").fetchone()
    return str(nome), str(email)


@pytest.fixture
def settings(db_path: Path, tmp_path: Path) -> Settings:
    return dataclasses.replace(Settings.from_env(), db_path=db_path, trace_path=tmp_path / "traces.jsonl")


# ------------------------------------------------------------------ unit


def test_same_row_shares_the_number() -> None:
    assert PEOPLE.mask_cell("Ana Lima") == "[nome_1]"
    assert PEOPLE.mask_cell("ana@example.com") == "[email_1]"
    assert PEOPLE.mask_cell("joao@example.net") == "[email_2]"


def test_mask_finds_values_inside_text_ignoring_case() -> None:
    masked = PEOPLE.mask("Quanto ana lima gastou? Escreva para joao@example.net.")
    assert masked == "Quanto [nome_1] gastou? Escreva para [email_2]."


def test_short_values_only_masked_as_whole_cells() -> None:
    assert PEOPLE.mask("Bom dia, Bo") == "Bom dia, Bo"  # "Bo" is too short to search inside text
    assert PEOPLE.mask_cell("Bo") == "[nome_3]"


def test_non_personal_cells_are_untouched() -> None:
    assert PEOPLE.mask_rows([["São Paulo", 6, None], ["Ana Lima", 1.5, "App"]]) == [
        ["São Paulo", 6, None],
        ["[nome_1]", 1.5, "App"],
    ]


def test_unmask_round_trip_and_unknown_tokens() -> None:
    assert PEOPLE.unmask("[nome_1] ([email_1]) e [nome_99]") == "Ana Lima (ana@example.com) e [nome_99]"


def test_unmask_sql_escapes_quotes() -> None:
    sql = "SELECT email FROM clientes WHERE nome = '[nome_2]'"
    assert PEOPLE.unmask_sql(sql) == "SELECT email FROM clientes WHERE nome = 'João D''Ávila'"


def test_empty_pseudonymizer_is_identity() -> None:
    empty = Pseudonymizer()
    assert not empty.active
    assert empty.mask("Ana Lima") == "Ana Lima" and empty.unmask("[nome_1]") == "[nome_1]"


def test_loads_every_client_from_the_database(db_path: Path) -> None:
    privacy = load_pseudonymizer(db_path, {"clientes.nome", "clientes.email", "clientes.nao_existe"})
    nome, email = first_client(db_path)
    assert privacy.mask_cell(nome) == "[nome_1]" and privacy.mask_cell(email) == "[email_1]"
    assert sum(1 for t in privacy.to_value if t.startswith("[email_")) == 100


# ---------------------------------------------------------- agent wiring


def test_prompt_without_personal_data_is_unchanged() -> None:
    plain = build_messages("Clientes por estado?", "ctx", [], {}, max_rows=10, remaining_steps=3)
    masked = build_messages("Clientes por estado?", "ctx", [], {}, max_rows=10, remaining_steps=3, privacy=PEOPLE)
    assert [m.content for m in plain] == [m.content for m in masked]  # the LLM cache stays valid


def test_llm_never_sees_names_or_emails(settings: Settings) -> None:
    nome, email = first_client(settings.db_path)
    sql = "SELECT nome, email FROM clientes ORDER BY rowid LIMIT 3"
    llm = FakeLLM([query(sql), answer(sql, "O primeiro é [nome_1] ([email_1]).")])
    response = Assistant(llm, settings).ask(f"Qual o e-mail de {nome}?")
    prompts = "\n".join(llm.prompts)
    assert nome not in prompts and email not in prompts
    assert "[nome_1]" in prompts and PROMPT_NOTE in prompts
    assert response.answer == f"O primeiro é {nome} ({email})."  # the user sees real values
    assert response.data is not None and response.data.rows[0][0] == nome  # table and CSV too


def test_model_sql_with_token_runs_on_the_real_value(settings: Settings) -> None:
    nome, email = first_client(settings.db_path)
    sql = "SELECT email FROM clientes WHERE nome = '[nome_1]'"
    llm = FakeLLM([query(sql), answer(sql, "É [email_1].")])
    response = Assistant(llm, settings).ask("Qual o e-mail do cliente 1?")
    assert response.status == "answered" and response.data is not None
    assert response.data.rows == [[email]]
    assert response.steps[0].sql == f"SELECT email FROM clientes WHERE nome = '{nome}'"  # shown real in the UI
    assert nome not in llm.prompts[-1] and "[nome_1]" in llm.prompts[-1]  # history goes back masked


def test_trace_keeps_question_and_sql_masked(settings: Settings) -> None:
    nome, _ = first_client(settings.db_path)
    sql = "SELECT email FROM clientes WHERE nome = '[nome_1]'"
    tracer = Tracer(settings.trace_path)
    Assistant(FakeLLM([query(sql), answer(sql, "ok")]), settings, tracer=tracer).ask(f"E-mail de {nome}?")
    raw = settings.trace_path.read_text(encoding="utf-8")
    assert nome not in raw and "[nome_1]" in raw


def test_empty_history_keeps_prompt_identical() -> None:
    messages = build_messages("Q?", "ctx", [], {}, max_rows=10, remaining_steps=3, history=())
    assert messages[1].content == (  # byte for byte the prompt before memory existed: the LLM cache stays valid
        "ctx\n\n# Pergunta do usuário\nQ?\n\n# Passos até agora\nNenhum passo executado ainda.\n\n"
        "Você ainda tem 3 decisão(ões). Decida o próximo passo."
    )


def test_history_is_masked(settings: Settings) -> None:
    nome, email = first_client(settings.db_path)
    sql = "SELECT email FROM clientes WHERE nome = '[nome_1]'"
    llm = FakeLLM([query(sql), answer(sql, "É [email_1]."), query(sql), answer(sql, "Sim, [email_1].")])
    assistant = Assistant(llm, settings)
    first = assistant.ask(f"Qual o e-mail de {nome}?", thread_id="pii")
    assistant.ask("Tem certeza?", thread_id="pii")

    assert first.answer == f"É {email}."  # the user still sees the real value
    turn = assistant.history("pii")[0]
    stored = turn.model_dump_json()
    assert nome not in stored and email not in stored
    assert turn.question == "Qual o e-mail de [nome_1]?" and "'[nome_1]'" in turn.sql
    assert turn.answer == "É [email_1]." and turn.status == "answered"
    assert nome not in llm.prompts[-1] and email not in llm.prompts[-1]
    assert "Pergunta: Qual o e-mail de [nome_1]?" in llm.prompts[-1]
