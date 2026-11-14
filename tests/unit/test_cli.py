"""The terminal entry point: ``python -m src.cli "pergunta"``."""

from __future__ import annotations

from pathlib import Path

import pytest

from src import cli
from src.llm import LLMError
from tests.unit.test_agent import P2_SQL, FakeLLM, answer, query


@pytest.fixture(autouse=True)
def _isolated(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("TRACE_PATH", str(tmp_path / "traces.jsonl"))
    monkeypatch.setenv("LLM_CACHE", "off")
    monkeypatch.setattr(cli, "load_env_file", lambda: None)


def test_prints_answer_steps_and_result(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    llm = FakeLLM([query(P2_SQL), answer(P2_SQL, "17 clientes.")])
    monkeypatch.setattr(cli, "get_llm", lambda: llm)
    assert cli.main(["Quantos clientes interagiram com WhatsApp em 2024?"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[answered] 17 clientes.")
    assert "passo 1 · query" in out and "COUNT(DISTINCT cliente_id)" in out
    assert "resultado (1 linhas" in out and "2 chamadas ao LLM" in out


def test_failed_answer_exits_1(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "get_llm", lambda: FakeLLM([LLMError("fake-llm: erro do provedor (HTTP 503)")]))
    assert cli.main(["Quantas compras?"]) == 1
    assert capsys.readouterr().out.startswith("[failed]")


def test_missing_llm_configuration_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def no_key() -> FakeLLM:
        raise LLMError("GEMINI_API_KEY não configurada")

    monkeypatch.setattr(cli, "get_llm", no_key)
    assert cli.main(["Quantas compras?"]) == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err
