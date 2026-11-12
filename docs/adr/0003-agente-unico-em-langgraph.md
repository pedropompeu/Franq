# ADR 0003 — Agente único em LangGraph com decisão estruturada por passo

- **Status:** aceito (2026-11-12)

## Contexto

A pergunta em linguagem natural precisa virar SQL, ser executada, corrigida quando falha e explicada. O LLM roda na cota gratuita do Gemini, então cada chamada conta. O avaliador precisa ver o raciocínio.

## Decisão

Um único agente em LangGraph com nós separados:

`load_context → decide → (execute ↺ decide | finalize | refuse)`

- `decide` chama o LLM, que devolve uma `AgentDecision` (Pydantic, saída estruturada): `run_query`, `final_answer` ou `refuse`, com raciocínio, SQL, resposta, premissas e proposta de gráfico.
- `execute` roda a SQL; um erro volta ao modelo como dado (mensagem real do SQLite) e conta como correção.
- `finalize` só aceita uma resposta que cite uma SQL já executada com sucesso. Se a SQL for nova, o sistema a executa e devolve o resultado ao modelo antes de aceitar.
- Limites: até 6 decisões do LLM e até 3 correções por pergunta. Ao estourar, a resposta é `failed`, nunca um número inventado.

## Alternativas descartadas

- **Multiagente** (planejador, gerador de SQL, crítico): mais chamadas por pergunta e mais pontos de falha, para um banco de 4 tabelas.
- **ReAct com tool-calling nativo**: acopla o fluxo ao formato de ferramenta de cada provedor. Com a decisão plana, o Gemini e o LLM falso dos testes usam o mesmo contrato (`StructuredLLM`).
- **Toolkit SQL pronto do LangChain**: opaco, sem validação por AST e com o erro como texto livre.

## Consequências

- Uma pergunta típica custa 2 chamadas (consulta + resposta); a avaliação mediu 1,9 em média.
- O fluxo é auditável passo a passo na interface e no trace.
- Memória de conversa (perguntas de acompanhamento) fica fora do escopo; ambiguidade é resolvida assumindo e declarando a premissa.
