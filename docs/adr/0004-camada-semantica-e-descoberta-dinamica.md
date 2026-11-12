# ADR 0004 — Camada semântica versionada e descoberta dinâmica do schema

- **Status:** aceito (2026-11-12)

## Contexto

O schema sozinho não diz o que o negócio quer dizer. O banco tem armadilhas verificadas: `clientes.valor_total_gasto` e `clientes.data_ultima_compra` divergem de `compras` em 100 de 100 clientes, e os dados vão de 2024-07-22 a 2025-07-22 (então "maio" é maio de 2025 e "2024" cobre só jul–dez).

## Decisão

- **Descoberta 100% dinâmica** no início de cada pergunta (com cache por caminho e data de modificação do banco): tabelas, colunas, chaves, colunas categóricas e seus valores (TEXT com até 30 valores e no máximo 50% de valores distintos), datas por formato, booleanas 0/1 e **data de referência = maior data do banco**. Nenhuma linha de amostra vai ao LLM.
- **`src/semantic/dictionary.yaml`** guarda só semântica de negócio: descrição de tabelas e colunas, métricas com a expressão da regra, colunas não confiáveis (`reliable: false`), colunas pessoais (`pii: true`) e regras de interpretação de datas. Ele é validado contra o schema real na inicialização.

## Alternativas descartadas

- **Valores categóricos escritos no YAML**: hardcode disfarçado; quebra quando os dados mudam.
- **Consultas completas no dicionário**: viraria um catálogo de respostas prontas, não um assistente.

## Consequências

- O agente usa a grafia exata dos valores (`'Reclamação'`, `'WhatsApp'`) e ignora as colunas não confiáveis (o caso P1 da avaliação testa essa armadilha).
- Os limiares da descoberta estão calibrados para este banco; num banco maior, o catálogo semântico deve ser a fonte primária.
