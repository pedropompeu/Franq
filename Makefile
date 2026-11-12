PY := .venv/bin/python

.PHONY: install lint typecheck test check run eval

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

# Avaliação com o Gemini real (gasta cota): make eval ARGS="--repeat 3"
eval:
	$(PY) -m src.evaluation $(ARGS)
