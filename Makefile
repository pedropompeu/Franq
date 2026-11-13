PY := .venv/bin/python

.PHONY: install lint typecheck test check run run-api eval demo-cache

install:
	python3 -m venv .venv
	$(PY) -m pip install -r requirements-dev.txt

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

typecheck:
	$(PY) -m mypy

test:
	$(PY) -m pytest

check: lint typecheck test

run:
	$(PY) -m streamlit run app.py

# API HTTP em http://localhost:8000 (documentação interativa em /docs)
run-api:
	$(PY) -m uvicorn src.api:app --reload

# Avaliação com o Gemini real (gasta cota): make eval ARGS="--repeat 3"
eval:
	$(PY) -m src.evaluation $(ARGS)

# Conversas da demo na ordem da tela: LLM_CACHE=on aquece, LLM_CACHE=replay confere
demo-cache:
	$(PY) -m src.demo_cache
