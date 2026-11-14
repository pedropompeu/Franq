# ADR 0008 — Memória de conversa por `thread_id`

- **Status:** aceito (2026-11-13); revisado em 2026-11-14 (histórico próprio no lugar do `InMemorySaver`)

## Contexto

O primeiro escopo deixou a memória de fora: cada pergunta era independente. O uso natural de um diretor, porém, é encadear perguntas ("Quantas reclamações não resolvidas?" e depois "E só no Chat?"). A segunda pergunta não faz sentido sem a primeira.

A memória não pode furar a pseudonimização ([ADR 0006](0006-pseudonimizacao-antes-do-llm.md)): o que o modelo vê de turnos anteriores precisa estar mascarado, e o estado guardado não deve acumular linhas de resultado com nomes e e-mails reais.

## Decisão (revisão de 2026-11-14)

- Cada `thread_id` é uma conversa. Na interface, uma thread por sessão do navegador; o botão "Nova conversa" troca de thread. Na API, o servidor emite o `thread_id` em `POST /conversations` e o prende à chave que o pediu ([ADR 0009](0009-api-fastapi-em-vez-de-langserve.md)).
- A memória é um `ConversationStore` próprio (`src/agent/memory.py`). Por conversa, ele guarda só a lista de `Turn`: pergunta, SQL e resposta **mascaradas** e o status. Linhas de resultado nunca entram.
- O grafo não tem checkpointer. `Assistant.ask` lê os turnos da conversa, passa-os ao grafo como `history` e grava o `turn` que o nó `remember` devolve. Por esse nó passam todos os caminhos de saída: resposta, recusa e falha.
- O store tem três limites: 500 conversas, com a usada há mais tempo saindo primeiro (LRU); 4 horas sem uso (TTL); 20 turnos por conversa.
- Perguntas da mesma conversa rodam uma de cada vez, por um lock por `thread_id`. Duas requisições simultâneas não leem o mesmo histórico nem embaralham os turnos. Uma conversa em uso não é descartada pelo LRU nem pelo TTL.
- O prompt mostra os 3 últimos turnos (`HISTORY_TURNS`) com uma nota: números vêm de consulta nova, nunca do histórico.
- Sem `thread_id` (CLI, avaliação), nada é guardado, e o prompt fica idêntico ao de antes da memória. Isso mantém o cache válido.

## Consequências

- Perguntas de acompanhamento funcionam: os casos M1 ("E só no Chat?") e M2 ("E pelo site?" depois de E1) da avaliação passam com o Gemini real.
- A RAM fica limitada: no máximo 500 × 20 turnos de texto mascarado, sem nenhum resultado de consulta.
- A memória some quando o processo reinicia, e cada réplica da API tem a sua. Com mais de uma réplica, o mesmo contrato (`turns`, `append`, `hold`) pode ser atendido por um Redis com TTL e lock distribuído.
- Uma ficha `[derivado_<n>]` de um turno anterior continua ficha na resposta, porque o valor real dela não está nos resultados do turno atual ([ADR 0006](0006-pseudonimizacao-antes-do-llm.md)).

## Histórico

- [2026-11-13] A primeira versão compilava o grafo com o `InMemorySaver` do LangGraph e uma lista explícita de tipos no serializador. ~superseded 2026-11-14 pela decisão acima. A auditoria externa de 2026-11-14 (itens 3.1 e 3.2) mostrou dois problemas. Os checkpoints guardavam cada estado intermediário, com as linhas reais do resultado, cerca de 8 por turno, sem limite de conversas nem de tempo. E nada impedia duas requisições na mesma thread de se cruzarem.
- [2026-11-13] Na API, o cliente escolhia o `thread_id`. ~superseded 2026-11-14: o servidor emite o id e o prende à chave (ADR 0009, auditoria item 2.6).

## Alternativas descartadas

- **Podar os checkpoints do `InMemorySaver`:** continua guardando estado que não é usado e não resolve a concorrência.
- **`SqliteSaver`:** dependência nova e linhas de resultado gravadas em disco.
- **`interrupt()` para pedir esclarecimento:** o agente assume a leitura padrão e declara a premissa; não interrompe o usuário.
- **Histórico com linhas de resultado:** mais tokens e dado pessoal no prompt; o modelo pode consultar de novo.
