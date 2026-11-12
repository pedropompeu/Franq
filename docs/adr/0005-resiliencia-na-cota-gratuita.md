# ADR 0005 — Resiliência para a cota gratuita do Gemini

- **Status:** aceito (2026-11-12)

## Contexto

O free tier limita requisições por minuto e por dia **por projeto e por modelo**. Em 2026-11-12: `gemini-3.5-flash-lite` e `gemini-3.1-flash-lite` com 15 RPM e 500 por dia cada; `gemini-2.5-flash` com só 20 por dia. O SDK às vezes devolve resposta vazia ou erro 5xx.

## Decisão

Tudo em `src/llm.py` e `src/resilience.py`, sem dependência nova e sem tocar no grafo:

- **Modelos em ordem de fallback** (`GEMINI_MODELS=gemini-3.5-flash-lite:15,gemini-3.1-flash-lite:15`), somando 1.000 chamadas por dia.
- **Limitador de RPM por modelo** antes de cada chamada.
- **Retry com backoff** em 5xx, timeout e 429 por minuto (respeitando o `retryDelay` da API); resposta vazia ou fora do schema também é tratada.
- **Marca persistida de "cota diária esgotada"** até a meia-noite do Pacífico: o modelo esgotado é pulado direto (faz o papel de circuit breaker para 429).
- **Cache em disco** por hash de modelo + mensagens + schema da saída (`LLM_CACHE=on`), e modo `replay` que só lê o cache e nunca chama a API.

## Alternativas descartadas

- **Várias chaves no mesmo projeto**: o limite é por projeto, não muda nada.
- **Modelo local (Ollama)**: exigiria GPU ou aceitaria qualidade bem menor.

## Consequências

- O app degrada com mensagem clara em vez de travar.
- Latências altas pontuais na avaliação vêm da espera do limitador local, não do SQL (1–3 ms).
