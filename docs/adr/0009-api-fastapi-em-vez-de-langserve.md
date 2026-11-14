# ADR 0009 — API própria em FastAPI, sem LangServe

- **Status:** aceito (2026-11-13), revisão de acesso em 2026-11-14

## Contexto

O assistente rodava só pela interface Streamlit e pela linha de comando. Um CRM, um contact center ou outro serviço precisa chamá-lo por HTTP, de preferência vendo o progresso da resposta (que leva alguns segundos e pode esperar pela cota do Gemini).

## Decisão

`src/api.py`, em FastAPI, expõe o mesmo `Assistant` da interface:

| Rota | O que faz |
|---|---|
| `GET /health` | Modelo e versão do prompt. Responde 503 quando o assistente não sobe (sem chave, modelo indisponível). Nunca pede chave. |
| `POST /conversations` | Abre uma conversa e devolve `{"thread_id"}`, gerado pelo servidor. |
| `POST /ask` | `{"question", "thread_id"?}` → `AgentResponse` em JSON. |
| `POST /ask/stream` | O mesmo em Server-Sent Events: um evento `progress` por etapa e, no fim, um `answer` ou um `error`. |

- Com `API_KEY` definida (uma ou mais, separadas por vírgula), toda rota exceto `/health` exige o cabeçalho `X-API-Key` (comparação em tempo constante). Sem ela, a API só atende loopback e endereços privados; de um endereço público, 403.
- O `thread_id` tem 128 bits aleatórios e pertence à chave que abriu a conversa. Um id que o servidor não emitiu para aquela chave recebe 404: adivinhar ou reaproveitar o id de outra chave não abre o histórico dela.
- `API_RATE_PER_MIN` perguntas por minuto por chave e endereço (padrão 10), em janela deslizante na memória do processo; acima disso, 429 com `Retry-After`.
- Erros do provedor do LLM chegam ao cliente como mensagem genérica (`erro do provedor (HTTP 400)`); o texto original, que pode ecoar a requisição, fica só no log.
- O `docker compose` publica as duas portas só em `127.0.0.1`.
- Os eventos de progresso são os mesmos `Progress` que a interface mostra ([`src/progress.py`](../../src/progress.py)); viram SSE sem adaptação.
- Um erro inesperado no stream vira um evento `error` genérico e um log com a pilha; nada de SQL ou pilha chega ao cliente.
- `make run-api` e o serviço `api` do `docker compose` (porta `${API_PORT:-8000}`). A interface Streamlit continua chamando o `Assistant` direto, sem salto de rede.

## Consequências

- Duas dependências novas, aprovadas antes: `fastapi` e `uvicorn` (o servidor ASGI padrão da documentação do FastAPI).
- A resposta da API traz valores reais (nomes, e-mails), como a tela. Só o LLM e os rastros veem fichas.
- O limite de requisições é por processo: com várias réplicas, cada uma conta o seu. Em produção, ficaria no gateway.
- Atrás de um proxy reverso todo cliente parece local, então ali a `API_KEY` é obrigatória.
- Uma chave compartilhada por vários usuários continua sendo um único dono: o que separa as conversas deles é o id aleatório.
- Se o cliente desconecta no meio do `/ask/stream`, a pergunta vai até o fim e o turno é gravado: uma execução do LangGraph não é interrompida com segurança no meio de um nó. O custo é cota gasta numa resposta que ninguém lê.

## Alternativas descartadas

- **LangServe:** o README do projeto diz "deprecated since Nov 18, 2024" e recomenda LangGraph Platform; o repositório foi arquivado em 2026-05-05 (lido em 2026-11-13). Além disso, o `Assistant` não é um `Runnable`: ele envolve o grafo com pseudonimização, rastro e memória.
- **LangGraph Platform:** serviço gerenciado e pago, desproporcional para um desafio com um único grafo.
- **Streamlit chamando a API:** um salto de rede a mais na demo, sem ganho para quem usa a tela.

## Histórico

- [2026-11-13] O cliente escolhia o `thread_id`; sem `API_KEY` a API atendia qualquer endereço; o compose publicava as portas em `0.0.0.0`; não havia limite de requisições; o texto cru do erro do provedor chegava aos passos da resposta. ~superseded 2026-11-14 pela decisão acima, após a auditoria externa de 2026-11-14 (itens 2.5, 2.6 e 2.8).
