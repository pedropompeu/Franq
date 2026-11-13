# ADR 0010 — Versão de prompt, gate de regressão e LangFuse opcional

- **Status:** aceito (2026-11-13)

## Contexto

O comportamento do agente depende do prompt, do dicionário de negócio e do schema da decisão. Sem uma versão, não dá para dizer qual prompt gerou uma resposta nem se uma mudança piorou a avaliação. Os rastros JSONL ([ADR 0007](0007-avaliacao-por-acerto-de-execucao.md)) ficam na máquina; uma equipe precisa ver gerações, custos e rodadas de avaliação num lugar só, sem expor dado pessoal ([ADR 0006](0006-pseudonimizacao-antes-do-llm.md)).

## Decisão

**Versão de prompt.** `src/versioning.py` calcula os 8 primeiros caracteres do sha256 de:

- os arquivos que montam as mensagens (`src/agent/prompts.py` e `src/privacy.py`, inteiros);
- o dicionário de negócio;
- o JSON schema do `AgentDecision`.

A versão vai em toda resposta (`AgentResponse.prompt_version`), em todo rastro, no `/health` da API e no cabeçalho do relatório de avaliação. Hashear os arquivos inteiros é grosseiro de propósito: uma edição de comentário também muda a versão, o que custa uma rodada de avaliação, nunca uma resposta com versão errada.

**Gate de regressão.** `make eval` compara a rodada com `docs/avaliacao.json` (taxa de acerto por pergunta) e sai com código **3** se alguma pergunta piorou (o 1 continua reservado para vazamento de dado pessoal ou banco alterado).

- Execuções perdidas por cota ou rede não contam como regressão nem entram na base.
- `--save-baseline` grava a rodada como nova base; com `--only`, atualiza só as perguntas rodadas.
- Uma rodada com vazamento ou banco alterado nunca vira base.

**LangFuse opcional.** Sem `LANGFUSE_PUBLIC_KEY` e `LANGFUSE_SECRET_KEY`, nada é enviado e o pacote `langfuse` nem é importado. Com elas:

- `TracedLLM` envolve a cadeia de modelos de `get_llm` e registra uma *generation* por decisão: mensagens, decisão, modelo, tokens e se veio do cache. Fica **abaixo** do grafo, então só vê as mensagens já mascaradas (`[nome_1]`). O cliente do LangFuse nunca entra no `config` do `invoke`, cujo estado tem as linhas reais.
- As generations de uma pergunta se agrupam por trace (semente = nosso `trace_id`), com a versão do prompt como tag e o `thread_id` como sessão.
- `make eval ARGS="--langfuse"` registra cada caso no dataset `indaga-eval` (pergunta e SQL de referência, já mascaradas) e uma execução com as notas `acerto`, `correcoes` e `latencia_ms`.
- Uma falha do LangFuse vira um aviso no log; a resposta ao usuário e a avaliação seguem.

## Consequências

- Uma dependência nova, aprovada antes: `langfuse==5.2.0`.
- Um teste garante que nenhuma mensagem enviada ao LangFuse contém nome ou e-mail real.
- O gate com `--repeat 1` pode acusar uma regressão falsa numa pergunta instável; a base é gravada com `--repeat 3`.
- Chamadas que falham (cota, rede) não aparecem no LangFuse; aparecem nos rastros JSONL.

## Alternativas descartadas

- **Callback do LangFuse no `invoke` do grafo:** veria o estado completo, com nomes e e-mails reais.
- **Versão manual do prompt (número no código):** depende de alguém lembrar de mudar.
- **Hash só do texto do `SYSTEM_PROMPT`:** deixava de fora a nota de conversa, a janela de histórico, a nota de privacidade e o modelo de mensagem.
- **LangSmith:** serviço pago fora do que a vaga cita; o LangFuse tem plano gratuito e pode rodar no próprio ambiente.
