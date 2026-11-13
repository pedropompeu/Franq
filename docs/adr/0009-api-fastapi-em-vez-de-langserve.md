# ADR 0009 — API própria em FastAPI, sem LangServe

- **Status:** aceito (2026-11-13)

## Contexto

O assistente rodava só pela interface Streamlit e pela linha de comando. Um CRM, um contact center ou outro serviço precisa chamá-lo por HTTP, de preferência vendo o progresso da resposta (que leva alguns segundos e pode esperar pela cota do Gemini).

## Decisão

`src/api.py`, em FastAPI, expõe o mesmo `Assistant` da interface:

| Rota | O que faz |
|---|---|
| `GET /health` | Modelo e versão do prompt. Responde 503 quando o assistente não sobe (sem chave, modelo indisponível). Nunca pede chave. |
| `POST /ask` | `{"question", "thread_id"?}` → `AgentResponse` em JSON. |
| `POST /ask/stream` | O mesmo em Server-Sent Events: um evento `progress` por etapa e, no fim, um `answer` ou um `error`. |

- Com `API_KEY` definida, toda rota exceto `/health` exige o cabeçalho `X-API-Key` (comparação em tempo constante).
- Os eventos de progresso são os mesmos `Progress` que a interface mostra ([`src/progress.py`](../../src/progress.py)); viram SSE sem adaptação.
- Um erro inesperado no stream vira um evento `error` genérico e um log com a pilha; nada de SQL ou pilha chega ao cliente.
- `make run-api` e o serviço `api` do `docker compose` (porta `${API_PORT:-8000}`). A interface Streamlit continua chamando o `Assistant` direto, sem salto de rede.

## Consequências

- Duas dependências novas, aprovadas antes: `fastapi` e `uvicorn` (o servidor ASGI padrão da documentação do FastAPI).
- A resposta da API traz valores reais (nomes, e-mails), como a tela. Só o LLM e os rastros veem fichas.
- Sem limite de requisições por cliente. Em produção, ficaria no gateway ou num middleware.
- Se o cliente desconecta no meio do `/ask/stream`, a pergunta vai até o fim e o turno é gravado: uma execução do LangGraph não é interrompida com segurança no meio de um nó. O custo é cota gasta numa resposta que ninguém lê.

## Alternativas descartadas

- **LangServe:** o README do projeto diz "deprecated since Nov 18, 2024" e recomenda LangGraph Platform; o repositório foi arquivado em 2026-05-05 (lido em 2026-11-13). Além disso, o `Assistant` não é um `Runnable`: ele envolve o grafo com pseudonimização, rastro e memória.
- **LangGraph Platform:** serviço gerenciado e pago, desproporcional para um desafio com um único grafo.
- **Streamlit chamando a API:** um salto de rede a mais na demo, sem ganho para quem usa a tela.
