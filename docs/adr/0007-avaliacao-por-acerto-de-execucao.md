# ADR 0007 — Avaliação por acerto de execução contra SQL de referência

- **Status:** aceito (2026-11-12), comparador revisto e conjunto congelado criado em 2026-11-14

## Contexto

Comparar o texto da SQL ou da resposta é frágil: há muitas consultas certas para a mesma pergunta. O modelo usado ignora `temperature`, então a mesma pergunta pode gerar SQL diferente a cada execução.

## Decisão

`src/evaluation.py` roda 20 perguntas de `tests/eval/questions.yaml` com o Gemini real, sem cache, e compara o **resultado** da consulta do agente com o de uma SQL escrita à mão:

- ordem das linhas e das colunas livre, colunas extras aceitas, exceto numa referência de valor único, onde o agente pode trazer uma coluna numérica e não várias;
- números com tolerância de 0,005, então arredondar para 2 casas vale e o ruído de ponto flutuante nunca reprova;
- rótulos `'YYYY-MM'` de um ano só também batem com o número do mês (`'01'`, `1`);
- quando a referência é um valor só, o texto da resposta precisa dizer esse valor;
- mais de uma referência aceita por caso (ex.: tendência em formato longo ou largo; as duas leituras do denominador na A1);
- modo `ranking` que aceita qualquer desempate na fronteira do corte (há três estados empatados no 5º lugar em maio de 2025), e também a lista de todos os empatados;
- premissa obrigatória nas perguntas ambíguas, e ela precisa citar o ponto ambíguo (`assumption_terms`);
- cada armadilha tem uma SQL errada no caso (`traps`), e um teste unitário garante que ela reprova nos dados reais;
- recusa obrigatória para fora do escopo, escrita e injeção;
- **autocorreção por falha injetada** no caso C1: a primeira SQL do modelo é embrulhada numa consulta com coluna inexistente, e o caso só passa se o modelo ler o erro do SQLite, corrigir e acertar;
- privacidade e integridade: nenhuma mensagem ao LLM com nome ou e-mail real, nenhum token sobrando na resposta e hash do banco igual antes e depois.

`--repeat` mede a variância entre execuções.

O `questions.yaml` é o **conjunto de ajuste**: o prompt e o dicionário mudaram olhando para ele. A medida fora da amostra é o **conjunto congelado** `tests/eval/paraphrases.yaml` (33 perguntas: as 20 com outra redação e 13 inéditas), escrito antes de rodar no modelo e travado por um teste com o SHA-256 do arquivo. Uma falha nele se corrige no prompt ou no código e se mede de novo no mesmo arquivo; uma referência errada vira um arquivo novo, e o antigo fica. Os relatórios trazem o intervalo de Wilson (95%) com n = perguntas, não execuções, porque as repetições de uma pergunta são correlacionadas.

## Alternativas descartadas

- **LLM como juiz**: gasta cota e não é verificável.
- **Esperar um erro natural para provar a autocorreção**: na primeira rodada real nenhuma consulta falhou sozinha; não é reprodutível.

## Consequências

- `make eval ARGS="--repeat 3"` gera `docs/avaliacao.md` e sai com código 1 se houver vazamento ou o banco mudar.
- Gasta cota real (~90 chamadas para 3 repetições; ~150 a mais para o conjunto congelado).

## Histórico

- [2026-11-12] Colunas extras sempre aceitas, números arredondados com `round(…, 2)`, `k` estrito no ranking, premissa checada só pela presença, texto da resposta nunca lido; a A1 e a E2 não distinguiam as leituras que deviam testar nestes dados. ~superseded 2026-11-14 pela decisão acima, após a auditoria externa de 2026-11-14 (itens 4.1, 4.4, 4.5 e 4.6).
