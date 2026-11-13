# ADR 0008 — Memória de conversa por `thread_id`

- **Status:** aceito (2026-11-13)

## Contexto

O primeiro escopo deixou a memória de fora: cada pergunta era independente. O uso natural de um diretor, porém, é encadear perguntas ("Quantas reclamações não resolvidas?" e depois "E só no Chat?"). A segunda pergunta não faz sentido sem a primeira.

A memória não pode furar a pseudonimização ([ADR 0006](0006-pseudonimizacao-antes-do-llm.md)): o que o modelo vê de turnos anteriores precisa estar mascarado, e o estado guardado não deve acumular linhas de resultado com nomes e e-mails reais.

## Decisão

- O grafo é compilado com o checkpointer `InMemorySaver` do LangGraph. Cada `thread_id` é uma conversa. Na interface, uma thread por sessão do navegador; o botão "Nova conversa" troca de thread. Na API, o cliente manda `thread_id` no corpo.
- Entre perguntas, só o campo `history` sobrevive. Todo o resto do estado é da pergunta e é zerado por `Assistant.ask`.
- Um nó `remember` no fim do grafo, por onde passam todos os caminhos de saída (resposta, recusa, falha), grava um `Turn`: pergunta, SQL e resposta **mascaradas** e o status. Linhas de resultado nunca entram no `Turn`.
- O prompt mostra os 3 últimos turnos (`HISTORY_TURNS`) com uma nota: números vêm de consulta nova, nunca do histórico.
- Sem `thread_id` (CLI, avaliação), a pergunta roda numa thread descartável e o prompt fica idêntico ao de antes da memória, o que mantém o cache válido.
- O serializador do checkpoint recebe a lista explícita dos tipos do estado (`allowed_msgpack_modules`). Um tipo novo no estado precisa entrar nessa lista; um teste falha se faltar.

## Consequências

- Perguntas de acompanhamento funcionam: os casos M1 ("E só no Chat?" depois de P4) e M2 ("E pelo site?" depois de E1) da avaliação passam com o Gemini real.
- A memória some quando o processo reinicia, e cada réplica da API tem a sua. Para mais de uma réplica: `SqliteSaver`/`PostgresSaver` com TTL, ou guardar só o `history`.
- O checkpoint da última pergunta de cada thread fica em RAM com o estado completo, inclusive as linhas reais do resultado. Não sai do processo, mas é o próximo ponto a enxugar.

## Alternativas descartadas

- **`SqliteSaver`:** dependência nova e linhas de resultado gravadas em disco.
- **`interrupt()` para pedir esclarecimento:** o agente assume a leitura padrão e declara a premissa; não interrompe o usuário.
- **Histórico com linhas de resultado:** mais tokens e dado pessoal no prompt; o modelo pode consultar de novo.
