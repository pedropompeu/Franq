# Indaga · Assistente Virtual de Dados

Pergunte em português sobre clientes, compras, suporte e campanhas. O assistente escreve a consulta SQL, executa em modo somente leitura, corrige a própria consulta quando ela falha e responde com texto, tabela, gráfico ou número, mostrando como chegou lá.

Solução do desafio técnico de Engenheiro(a) de IA Pleno:

- **Desafio 1** (implementado): assistente NL → SQL sobre o banco SQLite `data/anexo_desafio_1.db`.
- **Desafio 2** (arquitetura): pipeline para extrair dados de PDFs digitalizados. Ver [`docs/desafio2_arquitetura.md`](docs/desafio2_arquitetura.md).

![Resposta com gráfico de linhas: reclamações por mês e canal](docs/img/resposta.png)

## Como rodar

Requisitos: Python 3.12 e uma chave gratuita do Gemini ([Google AI Studio](https://aistudio.google.com/apikey)).

```bash
cp .env.example .env        # preencha só GOOGLE_API_KEY
make install                # cria .venv e instala dependências
make run                    # abre a interface em http://localhost:8501
```

Com Docker:

```bash
docker compose up --build   # http://localhost:8501
```

Pela linha de comando:

```bash
.venv/bin/python -m src.cli "Quantos clientes interagiram com campanhas de WhatsApp em 2024?"
```

Verificações e avaliação:

```bash
make check                  # ruff + formatação + mypy estrito + 190 testes (não usa a API)
make eval ARGS="--repeat 3" # avaliação com o Gemini real (gasta cota gratuita)
```

As demais variáveis são opcionais e estão documentadas em [`.env.example`](.env.example) (modelos de fallback, cache, limites de linhas e de tempo).

## Como funciona

Um único agente em [LangGraph](https://langchain-ai.github.io/langgraph/) com nós separados ([ADR 0003](docs/adr/0003-agente-unico-em-langgraph.md)):

```mermaid
flowchart LR
    Q([pergunta]) --> L[load_context<br/>schema descoberto +<br/>camada semântica]
    L --> D{decide<br/>Gemini}
    D -- consulta --> E[execute<br/>run_sql somente leitura]
    E -- erro ou dado insuficiente --> D
    E -- ok --> D
    D -- resposta --> F[finalize<br/>texto + visualização +<br/>premissas + consultas]
    D -- fora do escopo / perigoso --> R[refuse]
```

1. **Contexto dinâmico.** Tabelas, colunas, tipos, valores das colunas categóricas e intervalo de datas são descobertos do banco em tempo de execução, sem nada escrito à mão no prompt. Nenhuma linha de dado vai como exemplo ao modelo.
2. **Camada semântica.** [`src/semantic/dictionary.yaml`](src/semantic/dictionary.yaml) guarda só o significado de negócio: métricas como expressões, colunas não confiáveis, dados pessoais, convenções. É validado contra o schema real na inicialização ([ADR 0004](docs/adr/0004-camada-semantica-e-descoberta-dinamica.md)).
3. **Decisão estruturada.** A cada passo o Gemini devolve um `AgentDecision` (Pydantic): consultar, responder ou recusar. A resposta final traz texto, tipo de visualização, eixos, premissas e as consultas usadas.
4. **Autocorreção.** Se a SQL falha, o erro real do SQLite volta ao modelo. Limites: 6 passos e 3 correções por pergunta.
5. **Interface.** Streamlit mostra métrica, tabela ou gráfico (com download em CSV) e um painel "Como cheguei a esta resposta" com cada consulta, o grafo das tabelas e colunas usadas, a SQL e o custo em chamadas e tokens.

![Raciocínio: grafo da consulta, SQL e resposta](docs/img/raciocinio.png)

### Visão em grafo

A aba **Visão em grafo** mostra como estados, categorias de compra, canais de compra, canais de suporte e campanhas se ligam pelos mesmos clientes. Cada nó é um valor de uma dimensão, com tamanho proporcional ao número de clientes; cada ligação conta os clientes que têm as duas coisas. O grafo sai direto do SQLite, sem Neo4j e sem o LLM, e só mostra contagens: `nome` e `email` nunca são lidos. As dimensões vêm do schema real, e uma tabela com as ligações mais fortes acompanha o desenho. Renderizado com [pyvis](https://pyvis.readthedocs.io/) com o JavaScript embutido, sem CDN.

![Visão em grafo: estados e categorias de compra ligados por clientes em comum](docs/img/grafo.png)

### Armadilhas dos dados que o assistente trata

| Armadilha | Tratamento |
|---|---|
| `clientes.valor_total_gasto` e `clientes.data_ultima_compra` não batem com a tabela `compras` | Marcadas como não confiáveis no dicionário; totais e datas vêm de `compras` |
| Os dados vão de 2024-07-22 a 2025-07-22 | "Último ano", "em maio" e similares usam a data mais recente do banco, não a data de hoje. "Maio" = 2025-05 |
| "Em 2024" cobre só julho a dezembro | A resposta declara a cobertura real |
| `estado` está por extenso, não em sigla | Anotado no dicionário |
| Perguntas ambíguas ("média de compras por cliente") | O agente assume a leitura padrão e declara a premissa na resposta |

## Segurança

A execução de SQL tem defesa em profundidade ([ADR 0001](docs/adr/0001-guardrails-sql.md)): validação por AST com `sqlglot` (só um `SELECT`/`WITH`, sem `PRAGMA`, `ATTACH` ou escrita), conexão `mode=ro`, `PRAGMA query_only`, `set_authorizer` que nega qualquer ação fora de leitura, `set_progress_handler` como limite de tempo e limite de linhas aplicado pelo sistema (1.000 para a interface, 50 para o modelo). Pedidos de escrita, fora do escopo ou com injeção de prompt são recusados pelo agente antes de chegar ao banco.

### Dados pessoais

`nome` e `email` são dados pessoais ([ADR 0006](docs/adr/0006-pseudonimizacao-antes-do-llm.md)). Antes de qualquer texto ir ao Gemini, os valores reais viram fichas (`[nome_1]`, `[email_1]`). A SQL do modelo roda com os valores reais e quem usa o app vê nomes e e-mails de verdade. O modelo, o cache e os rastros só veem as fichas. A interface também avisa quando a resposta contém dados pessoais.

Limite conhecido: a pseudonimização cobre os valores que estão no banco. Um nome digitado na pergunta que não seja de cliente segue como texto.

## Resiliência na cota gratuita

O free tier do Gemini tem limite de requisições por minuto e por dia, por modelo ([ADR 0005](docs/adr/0005-resiliencia-na-cota-gratuita.md)):

- **Limitador local** de requisições por minuto, por modelo, antes de chamar a API.
- **Retry com backoff e jitter** em erros transitórios, incluindo resposta vazia do Gemini. O `retryDelay` informado pela API é respeitado.
- **Fallback real** para um segundo modelo Flash quando o primeiro esgota a cota (`GEMINI_MODELS`).
- **Registro de cota** em disco: um modelo esgotado não é chamado de novo até a cota zerar (meia-noite do Pacífico).
- **Cache** das respostas do LLM por hash da mensagem completa (`LLM_CACHE=on|off|replay`). Em `replay` o app só usa respostas já guardadas no cache local e nunca chama a API, nem precisa da chave.

## Observabilidade

Cada pergunta gera um rastro em `traces/traces.jsonl`: passos, SQL, erros, modelo, tokens, latência e se a chamada veio do cache. Os rastros guardam a pergunta e a SQL com as fichas no lugar de nomes e e-mails e nunca guardam linhas de resultado. A aba de rastros da interface lê esse arquivo.

## Avaliação

[`tests/eval/questions.yaml`](tests/eval/questions.yaml) tem 17 perguntas com SQL de referência escrita à mão: as 5 do enunciado, variações, dados pessoais, ambíguas, autocorreção com falha injetada, instrução maliciosa plantada no resultado da consulta e recusas. O acerto compara o **resultado** da consulta do agente com o da referência (acerto de execução), não o texto ([ADR 0007](docs/adr/0007-avaliacao-por-acerto-de-execucao.md)).

Última rodada, com o Gemini real, 3 execuções por pergunta e cache desligado ([relatório completo](docs/avaliacao.md)):

| Métrica | Valor |
|---|---|
| Acerto geral | 50/51 (98%) |
| Acerto de execução | 41/42 (98%) |
| Recusas corretas (fora do escopo, escrita, injeção) | 9/9 (100%) |
| Autocorreção após falha injetada | 3/3 (100%) |
| Instrução plantada nos dados ignorada | 3/3 (100%) |
| Mensagens ao LLM com nome ou e-mail real | 0 |
| Banco inalterado ao final | sim |
| Chamadas ao LLM por pergunta (média) | 1,9 |
| Latência por pergunta (mediana) | 2,6 s |

A única falha foi na pergunta ambígua A2 ("Quantos clientes compraram em julho?"): em 1 de 3 execuções, o agente somou os dois meses de julho dos dados, enquanto a referência usa o mais recente. No caso D1, uma ordem para ignorar as instruções e apagar a tabela `suporte` vai escondida numa coluna do resultado mostrado ao modelo. O modelo ignorou a ordem nas 3 execuções, e o `run_sql` só lê de qualquer forma.

Dezessete perguntas não provam robustez geral. A avaliação serve para pegar regressões nas armadilhas conhecidas, não como estimativa de acerto em produção.

### As 5 perguntas do enunciado

| Pergunta | Resposta |
|---|---|
| Liste os 5 estados com maior número de clientes que compraram via app em maio. | Maio de 2025: São Paulo (6), Minas Gerais e Santa Catarina (3) e dois entre Alagoas, Espírito Santo e Paraná, empatados com 2 |
| Quantos clientes interagiram com campanhas de WhatsApp em 2024? | 17 (os dados de 2024 cobrem julho a dezembro) |
| Quais categorias de produto tiveram o maior número de compras em média por cliente? | Roupas (2,21), Viagens (2,16), Livros (1,98), Serviços (1,96), Eletrônicos (1,92), Alimentos (1,88) |
| Qual o número de reclamações não resolvidas por canal? | Telefone 19, Chat 18, E-mail 14 |
| Qual a tendência de reclamações por canal no último ano? | Série mensal de julho de 2024 a julho de 2025, em gráfico de linhas (imagem acima) |

## Estrutura

```
app.py                     interface Streamlit
src/
  agent/                   grafo LangGraph, AgentDecision, prompts
  db/tools.py              run_sql com guardrails e descoberta do schema
  semantic/                dicionário de negócio e validação contra o schema
  llm.py, resilience.py    provedor, limitador, fallback, cache, registro de cota
  privacy.py               pseudonimização de nome e e-mail
  observability.py         rastros JSONL
  evaluation.py            avaliação por acerto de execução
  ui/                      componentes, textos, tema, grafo da consulta
  ui/banner.png            faixa do topo (imagem do material do desafio)
  ui/data_graph.py         aba "Visão em grafo" (pyvis, só agregados)
  cli.py                   uso pela linha de comando
tests/unit/                testes com LLM falso e determinístico
tests/eval/questions.yaml  perguntas de referência
docs/                      ADRs, avaliação, arquitetura do Desafio 2
```

## Decisões de arquitetura

Os ADRs estão em [`docs/adr/`](docs/adr/README.md). O banco de grafos (Neo4j) ficou de fora: são 4 tabelas pequenas num SQLite exigido pelo desafio ([ADR 0002](docs/adr/0002-sqlite-sem-grafo.md)). O grafo aparece onde ajuda a ler: no desenho de cada consulta e na aba "Visão em grafo", montada do próprio SQLite.

## Melhorias possíveis

- **Memória de conversa** para perguntas de acompanhamento ("e no Chat?").
- **LangFuse** opcional, ligado aos rastros JSONL e às rodadas de avaliação.
- **Catálogo semântico maior**, com sinônimos e métricas aprovadas pela área de negócio.
- **Mais perguntas de avaliação**, incluindo perguntas reais de usuários, e execução agendada da avaliação.
- **Pseudonimização também de nomes digitados** que não estão no banco (detecção de entidades).
- **Banco de grafos** (Neo4j ou similar) só se o modelo crescer para muitas entidades e relações; hoje a visão em grafo sai do SQLite.

## O que aprendi e o que mudei

Este projeto foi escrito do zero. Ele parte de limites que encontrei em projetos anteriores:

| Projeto anterior | Limite que encontrei | O que fiz aqui |
|---|---|---|
| `graphRag_PPGEGC-UFSC` (NL → Cypher, Streamlit, Gemini) | Schema escrito à mão no prompt, uma única geração sem autocorreção, cache em JSON | Schema descoberto em tempo de execução, loop de autocorreção com o erro real, cache por hash |
| `VagaFit` (LangGraph, golden set) | Avaliação com provedor falso não mede qualidade | Avaliação com o Gemini real por acerto de execução contra SQL de referência; rastros JSONL sempre ligados |
| `autou-case-analisador-email` (Gemini, retry, failover) | O failover caía num provedor simulado | Fallback para outro modelo de verdade e registro de cota que para de chamar um modelo esgotado |
| Todos | Sem validação de SQL por AST e sem camada semântica | `sqlglot` em camadas e dicionário de negócio versionado e validado |

O LLM falso continua existindo, mas só nos testes unitários.
