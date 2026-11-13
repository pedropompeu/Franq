from __future__ import annotations

import pytest

from src.config import Price, Settings, parse_price


def test_price_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_price("0.10,0.40") == Price(0.10, 0.40)
    assert parse_price(" 0.3 , 2.5 ") == Price(0.3, 2.5)
    monkeypatch.setenv("LLM_PRICE_USD_PER_MTOK", "0.10,0.40")
    assert Settings.from_env().price == Price(0.10, 0.40)
    monkeypatch.delenv("LLM_PRICE_USD_PER_MTOK")
    assert Settings.from_env().price is None


@pytest.mark.parametrize("raw", ["0.10", "a,b", "0.1,0.2,0.3", "-0.1,0.4", "nan,1", ""])
def test_price_env_rejects_invalid_values(raw: str) -> None:
    with pytest.raises(ValueError, match="LLM_PRICE_USD_PER_MTOK"):
        parse_price(raw)
