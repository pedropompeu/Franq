# Decisões de arquitetura (ADRs)

Registros curtos das escolhas que moldam o projeto: contexto, decisão, consequências e alternativas descartadas.

| ADR | Decisão |
|---|---|
| [0001](0001-guardrails-sql.md) | Execução de SQL somente leitura em camadas |
| [0002](0002-sqlite-sem-grafo.md) | SQLite relacional, sem banco de grafos |
| [0003](0003-agente-unico-em-langgraph.md) | Agente único em LangGraph com decisão estruturada por passo |
| [0004](0004-camada-semantica-e-descoberta-dinamica.md) | Camada semântica versionada e descoberta dinâmica do schema |
| [0005](0005-resiliencia-na-cota-gratuita.md) | Resiliência para a cota gratuita do Gemini |
| [0006](0006-pseudonimizacao-antes-do-llm.md) | Pseudonimização de nome e e-mail antes do LLM |
| [0007](0007-avaliacao-por-acerto-de-execucao.md) | Avaliação por acerto de execução contra SQL de referência |
