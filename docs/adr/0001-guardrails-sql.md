# ADR 0001 — Execução de SQL somente leitura em camadas

- **Status:** aceito (2026-11-12)

## Contexto
O agente gera SQL a partir de linguagem natural. Um modelo pode errar ou ser induzido (prompt injection, inclusive via conteúdo do próprio banco) a gerar escrita, múltiplas instruções ou consultas pesadas.

## Decisão
`src/db/tools.py::run_sql` aplica seis camadas independentes:
1. **AST com `sqlglot`**, por lista de permissão: exatamente uma instrução, com raiz `SELECT` ou `UNION`/`INTERSECT`/`EXCEPT`. Instruções que o parser não modela (`Command`) são rejeitadas.
2. **Conexão `mode=ro`.**
3. **`PRAGMA query_only = ON`.**
4. **Authorizer do `sqlite3`**: só `SELECT`, `READ`, `FUNCTION` e `RECURSIVE`. Barra `PRAGMA`, `ATTACH` e tabelas temporárias no próprio motor.
5. **Timeout** via `set_progress_handler`.
6. **Limite de linhas pelo sistema** (`fetchmany(max+1)`, com sinal de truncamento), sem reescrever a SQL do agente.

`run_sql` nunca lança exceção: devolve `QueryResult` ou `QueryError(kind=validation|execution|timeout)`, porque o loop de autocorreção consome o erro como dado.

## Consequências
- Testes de mutação mostram que cada camada tem cobertura própria: desligar qualquer uma faz testes falharem.
- Nenhuma dependência além do `sqlglot`. Authorizer, `query_only` e progress handler são da biblioteca padrão.
- O timeout é aproximado: é checado a cada 1.000 instruções da VM do SQLite.

## Alternativas descartadas
- **Só `sqlglot` + `mode=ro`:** uma única barreira lógica; um falso negativo do parser vira escrita.
- **`SQLDatabase`/`QuerySQLDataBaseTool` do LangChain:** sem validação por AST, erro devolvido como string solta, pouco testável.
- **Reescrever a query com `LIMIT`:** altera a SQL do agente e pode mudar a semântica (ex.: `ORDER BY` em subquery). `fetchmany` tem o mesmo efeito sem reescrita.
