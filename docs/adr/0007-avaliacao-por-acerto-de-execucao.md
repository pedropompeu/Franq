# ADR 0007 — Avaliação por acerto de execução contra SQL de referência

- **Status:** aceito (2026-11-12)

## Contexto

Comparar o texto da SQL ou da resposta é frágil: há muitas consultas certas para a mesma pergunta. O modelo usado ignora `temperature`, então a mesma pergunta pode gerar SQL diferente a cada execução.

## Decisão

`src/evaluation.py` roda 16 perguntas de `tests/eval/questions.yaml` com o Gemini real, sem cache, e compara o **resultado** da consulta do agente com o de uma SQL escrita à mão:

- ordem das linhas e das colunas livre, colunas extras aceitas, números com 2 casas;
- mais de uma referência aceita por caso (ex.: tendência em formato longo ou largo);
- modo `ranking` que aceita qualquer desempate na fronteira do corte (há três estados empatados no 5º lugar em maio de 2025);
- premissa obrigatória nas perguntas ambíguas;
- recusa obrigatória para fora do escopo, escrita e injeção;
- **autocorreção por falha injetada** no caso C1: a primeira SQL do modelo é embrulhada numa consulta com coluna inexistente, e o caso só passa se o modelo ler o erro do SQLite, corrigir e acertar;
- privacidade e integridade: nenhuma mensagem ao LLM com nome ou e-mail real, nenhum token sobrando na resposta e hash do banco igual antes e depois.

`--repeat` mede a variância entre execuções.

## Alternativas descartadas

- **LLM como juiz**: gasta cota e não é verificável.
- **Esperar um erro natural para provar a autocorreção**: na primeira rodada real nenhuma consulta falhou sozinha; não é reprodutível.

## Consequências

- `make eval ARGS="--repeat 3"` gera `docs/avaliacao.md` e sai com código 1 se houver vazamento ou o banco mudar.
- Gasta cota real (~90 chamadas para 3 repetições).
