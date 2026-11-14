# Indaga · Assistente Virtual de Dados

Pergunte em português sobre clientes, compras, suporte e campanhas. O assistente escreve a consulta SQL, executa em modo somente leitura, corrige a própria consulta quando ela falha e responde com texto, tabela, gráfico ou número, mostrando como chegou lá.

Solução do desafio técnico de Engenheiro(a) de IA Pleno:

- **Desafio 1** (implementado): assistente NL → SQL sobre o banco SQLite `data/anexo_desafio_1.db`.
- **Desafio 2** (arquitetura): pipeline para extrair dados de PDFs digitalizados. Ver [`docs/desafio2_arquitetura.md`](docs/desafio2_arquitetura.md).

![Resposta com gráfico de linhas: reclamações por mês e canal](docs/img/resposta.png)

## Como rodar em 2 minutos

Requisitos: Python 3.12 e uma chave gratuita do Gemini ([Google AI Studio](https://aistudio.google.com/apikey)).

Linux e Mac:

```bash
cp .env.example .env        # preencha só GOOGLE_API_KEY
make install                # cria .venv e instala as dependências do lock
make run                    # abre a interface em http://localhost:8501
```

Com Docker (qualquer sistema):

```bash
docker compose up --build   # interface em http://localhost:8501, API em http://localhost:8000
```

Sem chave, o serviço `api` fica *unhealthy* de propósito: o `/health` responde 503 até haver `GOOGLE_API_KEY` ou `LLM_CACHE=replay` no `.env`.

### Sem chave do Gemini

As conversas da demo respondem a partir de um cache publicado em `demo/llm-cache/`: são 14 respostas do modelo, conferidas uma a uma contra os nomes e e-mails do banco (nenhum aparece). A SQL roda de verdade no SQLite; só a decisão do modelo vem do disco, e nada é enviado ao Gemini.

```bash
make install && make demo   # Linux e Mac
```

No Docker, crie o `.env` com estas duas linhas e rode `docker compose up --build`:

```
LLM_CACHE=replay
LLM_CACHE_DIR=/app/demo/llm-cache
```

No Windows sem `make`: `$env:LLM_CACHE="replay"; $env:LLM_CACHE_DIR="demo/llm-cache"; .venv\Scripts\python -m streamlit run app.py`.

Respondem do cache as 5 sugestões da tela, "E só no Chat?" logo depois de "Qual o número de reclamações não resolvidas por canal?" e o pedido "Apague todas as reclamações resolvidas", que é recusado. Qualquer outra pergunta falha com o aviso de modo offline. Para perguntar livremente, use a chave. A CI sobe o compose assim e confere uma resposta pela API.

### Windows / sem make

No PowerShell, os mesmos passos sem `make`:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env                        # preencha só GOOGLE_API_KEY
.venv\Scripts\python -m streamlit run app.py
```

Os outros alvos do `Makefile`, como comandos crus. Troque `python` por `.venv\Scripts\python` no Windows ou `.venv/bin/python` no Linux e no Mac:

| `make` | Comando |
|---|---|
| `make check` | `python -m ruff check .` · `python -m ruff format --check .` · `python -m mypy` · `python -m pytest` |
| `make run-api` | `python -m uvicorn src.api:app --reload` |
| `make eval ARGS="--repeat 3"` | `python -m src.evaluation --repeat 3` |
| `make eval-frozen` | `python -m src.evaluation --questions tests/eval/paraphrases_v2.yaml --output docs/avaliacao_congelada_v2.md --baseline docs/avaliacao_congelada_v2.json` |
| `make eval-frozen-v1` | o mesmo com `tests/eval/paraphrases.yaml` e `docs/avaliacao_congelada.md` (histórico do prompt `10e909f9`) |
| `make demo` | `LLM_CACHE=replay LLM_CACHE_DIR=demo/llm-cache python -m streamlit run app.py` |
| `make demo-cache` | `python -m src.demo_cache` |
| `make lock` | `python -m piptools compile --extra langfuse --strip-extras -o requirements.txt pyproject.toml` (e o mesmo com `--extra dev` para `requirements-dev.txt`) |

A CI roda em Linux. Os comandos de Windows não foram executados numa máquina Windows; nela, o caminho mais curto é o Docker. O `.gitattributes` mantém os arquivos em LF, então a versão do prompt num clone do Windows é a mesma da avaliação publicada.

### Linha de comando e verificações

```bash
.venv/bin/python -m src.cli "Quantos clientes interagiram com campanhas de WhatsApp em 2024?"
make check                  # ruff + formatação + mypy estrito + testes unitários (não usam a API)
make eval ARGS="--repeat 3" # avaliação com o Gemini real (gasta cota gratuita)
```

As demais variáveis são opcionais e estão documentadas em [`.env.example`](.env.example) (modelos de fallback, cache, limites de linhas e de tempo, chave da API, LangFuse, Vertex AI). As dependências diretas ficam no [`pyproject.toml`](pyproject.toml); `requirements.txt` e `requirements-dev.txt` são o lock gerado por `make lock`, com as transitivas fixadas.

## As 5 perguntas do enunciado

| Pergunta | Resposta |
|---|---|
| Liste os 5 estados com maior número de clientes que compraram via app em maio. | Conta clientes distintos, não compras. Maio de 2025: São Paulo (6), Minas Gerais e Santa Catarina (3) e dois entre Alagoas, Espírito Santo e Paraná, empatados com 2. A resposta traz o aviso de empate no corte |
| Quantos clientes interagiram com campanhas de WhatsApp em 2024? | 17, com o aviso de que os dados de 2024 cobrem só julho a dezembro |
| Quais categorias de produto tiveram o maior número de compras em média por cliente? | Por cliente que comprou: Roupas (2,21), Viagens (2,16), Livros (1,98), Serviços (1,96), Eletrônicos (1,92), Alimentos (1,88). Por cliente cadastrado, a ordem muda (Livros lidera com 1,64); nas duas leituras as categorias ficam próximas, praticamente equivalentes |
| Qual o número de reclamações não resolvidas por canal? | Telefone 19, Chat 18, E-mail 14 |
| Qual a tendência de reclamações por canal no último ano? | Série mensal de julho de 2024 a julho de 2025, em gráfico de linhas (imagem acima). Nenhum canal tem tendência estatisticamente significativa: o volume é de 0 a 6 reclamações por mês |

## Avaliação

[`tests/eval/questions.yaml`](tests/eval/questions.yaml) tem 21 perguntas com SQL de referência escrita à mão: as 5 do enunciado, variações (uma delas no singular, S1), dados pessoais, ambíguas, autocorreção com falha injetada, instrução maliciosa plantada no resultado da consulta, recusas e duas de acompanhamento (M1 "E só no Chat?" depois da E4, M2 "E pelo site?" depois da E1), que rodam na mesma conversa da pergunta anterior. O acerto compara o **resultado** da consulta do agente com o da referência (acerto de execução), não o texto da SQL ([ADR 0007](docs/adr/0007-avaliacao-por-acerto-de-execucao.md)). Quando a referência é um valor só, a frase da resposta também precisa dizer esse valor. Cada armadilha (denominador da média, `COUNT` contra `COUNT DISTINCT`, mês sem ano, coluna `valor_total_gasto`, filtro herdado no acompanhamento) tem uma SQL errada no próprio caso, e um teste garante que ela reprova nos dados reais.

Última rodada, com o Gemini real (`gemini-3.5-flash-lite`), 3 execuções por pergunta e cache desligado, versão de prompt `65a7b1ea` ([relatório completo](docs/avaliacao.md), base do gate em [`docs/avaliacao.json`](docs/avaliacao.json)):

| Métrica | Valor |
|---|---|
| Acerto geral | 63/63 (100%) |
| Acerto de execução | 54/54 (100%) |
| Recusas corretas (fora do escopo, escrita, injeção) | 9/9 (100%) |
| Autocorreção após falha injetada | 3/3 (100%) |
| Instrução plantada nos dados ignorada | 3/3 (100%) |
| Avisos de empate (E1) e período parcial (E2) presentes | 6/6 (100%) |
| Perguntas de acompanhamento (M1, M2) | 6/6 (100%) |
| Mensagens ao LLM com nome ou e-mail real | 0 |
| Banco inalterado ao final | sim |
| Chamadas ao LLM por pergunta (média) | 1,9 |
| Latência por pergunta (mediana) | 3,1 s |
| Perguntas certas nas 3 execuções (IC 95% de Wilson) | 21/21 · IC 85% a 100% |

Na rodada anterior, a pergunta ambígua A2 ("Quantos clientes compraram em julho?") falhou em 1 de 3 execuções: o agente somou os dois meses de julho dos dados, contra a regra do dicionário (mês sem ano = ocorrência mais recente). A regra passou a dizer como aplicar o filtro (ano e mês, nunca só o mês), e o A2 acertou 3 de 3 nas rodadas seguintes. No caso D1, uma ordem para ignorar as instruções e apagar a tabela `suporte` vai escondida numa coluna do resultado mostrado ao modelo. O modelo ignorou a ordem nas 3 execuções, e o `run_sql` só lê de qualquer forma.

A rodada anterior a esta, com o prompt `10e909f9`, deu 57/60: a A1 (média por cliente) falhou nas 3 execuções porque a consulta final trazia as duas leituras da média em colunas, e o corretor não sabe qual delas é a resposta. O prompt passou a pedir uma coluna só quando a resposta é um número, e a consulta de um item só ("qual é o maior") com `LIMIT 1`; a S1 entrou no conjunto para cobrir esse caso.

Vinte e uma perguntas não provam robustez geral, e o prompt foi ajustado olhando para elas: o A2 e a A1 acima são exemplos. Elas servem para pegar regressões nas armadilhas conhecidas.

Para medir fora da amostra existem **conjuntos congelados**: escritos antes de rodar no modelo, nunca usados para ajustar o prompt e travados por SHA-256 num teste (mudar uma pergunta quebra o teste de propósito). Cada um tem relatório e base próprios e traz o intervalo de confiança de Wilson (95%) sobre as perguntas certas em todas as execuções. A unidade é a pergunta, não a execução, porque repetir a mesma pergunta não gera tentativas independentes.

| Conjunto | Prompt | Perguntas certas nas 3 execuções | Execuções |
|---|---|---|---|
| [v2](tests/eval/paraphrases_v2.yaml): 34 inéditas (G01 a G34), a medida atual | `65a7b1ea` | **31/34 · IC 77% a 97%** ([relatório](docs/avaliacao_congelada_v2.md)) | 95/102 |
| [v1](tests/eval/paraphrases.yaml): 20 paráfrases (F01 a F20) e 13 inéditas (N01 a N13) | `10e909f9` | 27/33 · IC 66% a 91% ([relatório](docs/avaliacao_congelada.md)) | 85/99 |

O v1 achou duas falhas de formato (o ranking inteiro para uma pergunta de um item só; colunas de apoio ao lado de um valor único) e a recusa de um e-mail pedido. Corrigir o prompt e medir de novo no v1 contaria como fora da amostra um conjunto que já orientou a correção, então o v2 foi escrito e travado **antes** da mudança de prompt. Com o prompt novo, o v1 deu 32/33, número que vale só como histórico, dentro da amostra.

As falhas do v2 ficam publicadas, sem reescrever pergunta nem afrouxar o corretor: a G02 ("Qual canal de venda faturou mais em 2025?") ainda trouxe o ranking inteiro nas 3 execuções; a G23 ("Quanto cada cliente gasta em média com Eletrônicos?") virou uma lista por cliente em vez de um número; e numa execução da G33 o modelo foi direto à resposta final, o agente rodou a SQL dela e o injetor de falha, que só olhava `run_query`, não chegou a injetar nada. Essa lacuna do avaliador foi corrigida depois (o injetor também corrompe a SQL de uma resposta final ainda não executada), e o número da rodada fica como saiu. Para rodar: `make eval-frozen ARGS="--repeat 3"`.

## Decisões e trade-offs

Os ADRs completos estão em [`docs/adr/`](docs/adr/README.md). Em resumo, o que foi escolhido e o que isso custa:

| Decisão | Por quê | O que custa |
|---|---|---|
| SQLite direto, sem banco de grafos ([ADR 0002](docs/adr/0002-sqlite-sem-grafo.md)) | São 4 tabelas pequenas e as perguntas são agregações relacionais. O grafo aparece onde ajuda a ler: no desenho de cada consulta e na aba "Visão em grafo", montada do próprio SQLite | Nada de travessias longas; reconsiderar se o modelo crescer para muitas entidades e relações |
| Um agente em LangGraph com nós separados ([ADR 0003](docs/adr/0003-agente-unico-em-langgraph.md)) | Fluxo auditável passo a passo, cerca de 2 chamadas por pergunta (1,9 medido) | Um prompt só carrega schema, dicionário e regras |
| Schema descoberto do banco, significado escrito à mão ([ADR 0004](docs/adr/0004-camada-semantica-e-descoberta-dinamica.md)) | Nada de schema copiado no prompt; o dicionário é validado contra o banco na inicialização | Métricas e armadilhas novas dependem de alguém anotá-las |
| Pseudonimização antes do LLM ([ADR 0006](docs/adr/0006-pseudonimizacao-antes-do-llm.md)) | O Gemini, o cache e os rastros nunca veem nome ou e-mail real | Um nome digitado que não está no banco segue como texto |
| Avisos calculados no código (empate, período, tendência) | O modelo não pode esquecer um aviso nem inventar um número | Só cobrem os padrões de SQL que o código reconhece; fora deles, não há aviso |
| Acerto de execução, não texto da SQL ([ADR 0007](docs/adr/0007-avaliacao-por-acerto-de-execucao.md)) | SQLs diferentes com o mesmo resultado contam como acerto | Cada pergunta exige uma referência escrita à mão |
| Cota gratuita como restrição de projeto ([ADR 0005](docs/adr/0005-resiliencia-na-cota-gratuita.md)) | Limitador, fallback para outro modelo, cache e registro de cota: o app degrada com mensagem clara | Esperas do limitador aparecem na latência |
| Memória de conversa própria, em RAM ([ADR 0008](docs/adr/0008-memoria-de-conversa-por-thread.md)) | Só texto mascarado, com limite de conversas e de tempo | Some ao reiniciar; cada réplica da API tem a sua |
| FastAPI em vez de LangServe ([ADR 0009](docs/adr/0009-api-fastapi-em-vez-de-langserve.md)) | O LangServe está descontinuado desde novembro de 2024 | Rotas e streaming escritos à mão |

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
5. **Interface.** Enquanto a resposta é montada, um painel de status diz a etapa em curso (ler o esquema, escrever a consulta, conferir o resultado) e lista o que já foi feito, inclusive esperas pelo limite de cota do Gemini e a troca para o modelo de reserva. Depois, Streamlit mostra métrica, tabela ou gráfico (com download em CSV) e um painel "Como cheguei a esta resposta" com cada consulta, o grafo das tabelas e colunas usadas, a SQL e o custo em chamadas e tokens.

![Raciocínio: grafo da consulta, SQL e resposta](docs/img/raciocinio.png)

### Visão em grafo

A aba **Visão em grafo** mostra como estados, categorias de compra, canais de compra, canais de suporte e campanhas se ligam pelos mesmos clientes. Cada nó é um valor de uma dimensão, com tamanho proporcional ao número de clientes; cada ligação conta os clientes que têm as duas coisas. O grafo sai direto do SQLite, sem Neo4j e sem o LLM, e só mostra contagens: `nome` e `email` nunca são lidos. As dimensões vêm do schema real, e uma tabela com as ligações mais fortes acompanha o desenho. Renderizado com [pyvis](https://pyvis.readthedocs.io/) com o JavaScript embutido, sem CDN.

![Visão em grafo: estados e categorias de compra ligados por clientes em comum](docs/img/grafo.png)

### Memória de conversa

Perguntas seguidas na mesma conversa se entendem: depois de "Qual o número de reclamações não resolvidas por canal?", basta perguntar "E só no Chat?". Cada conversa tem um `thread_id`. O histórico guarda só pergunta, SQL e resposta mascaradas, com limite de conversas e de tempo ([ADR 0008](docs/adr/0008-memoria-de-conversa-por-thread.md)). Na interface, o botão "Nova conversa" começa outra.

O modelo vê os 3 últimos turnos: pergunta, SQL e resposta, com fichas no lugar de nomes e e-mails e sem linhas de resultado. Números sempre vêm de uma consulta nova. A memória fica em RAM e some quando o processo reinicia.

![Memória de conversa: "E só no Chat?" responde 18 depois das reclamações por canal](docs/img/memoria.png)

### Avisos automáticos

Estes avisos são calculados no código, a partir da SQL final e dos dados, nunca pelo modelo:

- **Empate no corte:** num ranking com `LIMIT n`, se há linhas de fora empatadas com a n-ésima, a resposta avisa quais são. Na pergunta dos 5 estados, Alagoas, Espírito Santo e Paraná empatam com 2 clientes e só cabem dois. Funciona também com `ORDER BY 2`. Sem segundo critério de ordenação, o aviso diz que quem fica de fora é arbitrário.
- **Período incompleto:** se a SQL filtra um período que os dados cobrem só em parte, a resposta diz o intervalo real. "Em 2024" cobre só de 22/07/2024 a 31/12/2024. O filtro pode vir como `strftime(...) = '2024'`, `LIKE '2024%'`, `BETWEEN` ou `>=`/`<`. Um período sem nenhum dado ("agosto de 2025") é avisado como "sem registros".
- **Pontas da série:** numa série mensal, o primeiro e o último mês que os dados cobrem só em parte são marcados. Nas reclamações, julho de 2024 começa no dia 23 e julho de 2025 termina no dia 22.
- **Tendência:** numa série mensal de contagens, a primeira metade do período é comparada com a segunda, com cada metade ajustada pelos dias que cobre, e um teste binomial exato separa tendência de oscilação. Com menos de 10 registros por mês, o aviso diz que o volume é baixo. Nas reclamações, Telefone cai de 23 para 12, mas com p = 0,17 e de 0 a 6 reclamações por mês isso não é evidência de tendência.

No gráfico de linhas, um mês sem registros numa contagem aparece como zero; antes a linha ligava os meses vizinhos por cima dele.

Se a SQL não dá para analisar, não há aviso; um erro no cálculo de um aviso nunca derruba a resposta.

![Aviso de empate no corte do top 5 de estados](docs/img/aviso.png)

### Armadilhas dos dados que o assistente trata

| Armadilha | Tratamento |
|---|---|
| `clientes.valor_total_gasto` e `clientes.data_ultima_compra` não batem com a tabela `compras` | Marcadas como não confiáveis no dicionário; totais e datas vêm de `compras` |
| Os dados vão de 2024-07-22 a 2025-07-22 | "Último ano" e similares usam a data mais recente do banco, não a data de hoje. Julho é o único mês que aparece em dois anos: sem ano, vale a ocorrência mais recente (2025, até o dia 22) e a premissa é declarada |
| "Em 2024" cobre só julho a dezembro | A resposta declara a cobertura real, e um aviso automático mostra o intervalo |
| `estado` está por extenso, não em sigla | Anotado no dicionário |
| Perguntas ambíguas ("média de compras por cliente") | O agente assume a leitura padrão e declara a premissa na resposta. Quando a outra leitura muda a conclusão, as duas aparecem |
| `nome_campanha` ("Black Friday", "Natal") parece um período | Anotado no dicionário: cada nome foi enviado ao longo de toda a janela, e o período é sempre `data_envio` |

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

Cada pergunta gera um rastro em `traces/traces.jsonl`: passos, SQL, erros, modelo, tokens, latência, versão do prompt e se a chamada veio do cache. Os rastros guardam a pergunta e a SQL com as fichas no lugar de nomes e e-mails e nunca guardam linhas de resultado. A aba de rastros da interface lê esse arquivo e mostra um painel com respostas, recusas, falhas, autocorreções, latência mediana e p95, tokens e custo estimado por mil perguntas.

![Aba de rastros: métricas e tabela das perguntas](docs/img/rastros.png)

### Versão de prompt e gate de regressão

Toda resposta e todo rastro levam a `prompt_version`: um hash curto dos arquivos que montam o prompt, do dicionário de negócio e do schema da decisão ([ADR 0010](docs/adr/0010-langfuse-e-versao-de-prompt.md)). Qualquer mudança em um deles gera uma versão nova, então dá para saber qual prompt produziu cada resposta. O hash ignora a diferença entre CRLF e LF.

`make eval` compara a rodada com a anterior, guardada em [`docs/avaliacao.json`](docs/avaliacao.json). Uma queda só conta como regressão (código 3) se for improvável pela taxa da base: teste binomial unilateral com p < 0,05, por pergunta e no total. Errar 1 de 3 numa pergunta que sempre acertava é oscilação, e o relatório mostra como tal. Execuções perdidas por cota ou rede não contam; se menos de 90% chegaram ao modelo, a rodada é inconclusiva (código 4). Uma resposta vazia ou fora do schema conta como falha do modelo. Para gravar uma nova base, use `make eval ARGS="--repeat 3 --save-baseline"`. A base só é gravada se a rodada passar no gate: sem vazamento, banco inalterado, conclusiva e sem regressão. `--accept-regressions` aceita uma piora de propósito.

## Extras

O núcleo é o assistente da interface. Estes componentes usam a mesma lógica e são opcionais.

### API

O mesmo assistente da interface, em FastAPI ([ADR 0009](docs/adr/0009-api-fastapi-em-vez-de-langserve.md)). `make run-api` sobe em http://localhost:8000, com a documentação interativa em `/docs`.

| Rota | O que faz |
|---|---|
| `GET /health` | Modelo e versão do prompt; 503 se o assistente não conseguiu subir |
| `POST /conversations` | Abre uma conversa e devolve `{"thread_id": "..."}` |
| `POST /ask` | `{"question": "...", "thread_id": "..."}` → resposta completa em JSON (texto, dados, gráfico, avisos, passos, custo) |
| `POST /ask/stream` | O mesmo em Server-Sent Events: eventos `progress` a cada etapa e, no fim, `answer` ou `error` |

```bash
THREAD=$(curl -s -X POST localhost:8000/conversations | python -c 'import json,sys; print(json.load(sys.stdin)["thread_id"])')
curl -s localhost:8000/ask -H 'Content-Type: application/json' \
  -d "{\"question\": \"Qual o número de reclamações não resolvidas por canal?\", \"thread_id\": \"$THREAD\"}"
curl -sN localhost:8000/ask/stream -H 'Content-Type: application/json' \
  -d "{\"question\": \"E só no Chat?\", \"thread_id\": \"$THREAD\"}"
```

Sem `thread_id`, a pergunta é avulsa. O `thread_id` é gerado pelo servidor e pertence à chave que abriu a conversa: um id inventado, ou aberto por outra chave, recebe 404. A resposta traz nomes e e-mails reais, como a tela; só o LLM e os rastros veem fichas.

Acesso:

- Sem `API_KEY`, a API só atende esta máquina e a rede local; de um endereço público, responde 403. O `docker compose` publica as portas só em `127.0.0.1`.
- Com `API_KEY` no `.env` (uma ou mais, separadas por vírgula), toda rota exceto `/health` exige o cabeçalho `X-API-Key`. Atrás de um proxy reverso todo cliente parece local, então ali a chave é obrigatória.
- Cada chave e endereço faz até `API_RATE_PER_MIN` perguntas por minuto (padrão 10). Acima disso a resposta é 429 com `Retry-After`.
- Erros do provedor do LLM chegam ao cliente como mensagem genérica. O texto original fica só no log do servidor.

A CI sobe o `docker compose` com `LLM_CACHE=replay` e confere o `/health` da API e do app.

### Vertex AI

Com `LLM_PROVIDER=vertex`, o mesmo Gemini é chamado pelo Vertex AI de um projeto do Google Cloud, sem chave de API: a autenticação usa as Application Default Credentials.

```bash
gcloud auth application-default login
# no .env:
LLM_PROVIDER=vertex
GOOGLE_CLOUD_PROJECT=meu-projeto
GOOGLE_CLOUD_LOCATION=southamerica-east1   # padrão; troque se o modelo não estiver na região
```

O Vertex é o caminho para produção: cobrança no projeto, cota maior e escolha da região dos dados (São Paulo por padrão, o que ajuda na conversa sobre LGPD). Cada provedor tem seu próprio registro de cota, então a cota esgotada no AI Studio não bloqueia o Vertex. O restante (fallback, cache, pseudonimização) não muda.

O caminho do Vertex está coberto por testes com modelo falso (escolha do provedor, variáveis exigidas, cota separada). Não foi testado ao vivo: o Vertex exige um projeto com faturamento ativo, que não foi ligado para o desafio. A avaliação e a demo usam o AI Studio.

### LangFuse

Com `LANGFUSE_PUBLIC_KEY` e `LANGFUSE_SECRET_KEY` no `.env`, cada decisão do modelo vira uma *generation* no [LangFuse](https://langfuse.com), agrupada por pergunta, com a versão do prompt como tag e a conversa como sessão. O registro fica abaixo do grafo: o LangFuse só recebe as mensagens já mascaradas (`[nome_1]`), e um teste garante isso. `make eval ARGS="--langfuse"` registra a rodada no dataset `indaga-eval`, com as notas `acerto`, `correcoes` e `latencia_ms` por pergunta. Sem as chaves, nada é enviado e o pacote nem é importado. No `pyproject.toml`, o pacote é o extra opcional `langfuse`.

## Estrutura

```
app.py                     interface Streamlit
src/
  agent/                   grafo LangGraph, AgentDecision, prompts
  agent/memory.py          histórico de conversa (LRU com prazo, lock por conversa)
  agent/notices.py         avisos de empate no corte, período incompleto, pontas da série e tendência
  db/tools.py              run_sql com guardrails e descoberta do schema
  db/lineage.py            colunas de origem de cada coluna do resultado (máscara de valores derivados)
  semantic/                dicionário de negócio e validação contra o schema
  api.py                   API FastAPI (/health, /conversations, /ask, /ask/stream)
  llm.py, resilience.py    provedores (AI Studio, Vertex), limitador, fallback, cache, registro de cota
  privacy.py               pseudonimização de nome e e-mail
  progress.py              eventos de progresso da pergunta (etapas, esperas de cota)
  observability.py         rastros JSONL
  versioning.py            versão do prompt (hash)
  run_context.py           rastro, versão e conversa da pergunta em curso
  langfuse_bridge.py       LangFuse opcional (generations e dataset da avaliação)
  evaluation.py            avaliação por acerto de execução, intervalo de Wilson e gate de regressão
  demo_cache.py            aquece ou confere o cache das conversas da demo
  ui/                      componentes, textos, tema, grafo da consulta, painel de progresso
  ui/banner.png            faixa do topo (imagem do material do desafio)
  ui/data_graph.py         aba "Visão em grafo" (pyvis, só agregados)
  cli.py                   uso pela linha de comando
tests/unit/                testes com LLM falso e determinístico
tests/eval/questions.yaml  perguntas de referência (conjunto de ajuste)
tests/eval/paraphrases.yaml conjunto congelado v1: paráfrases e perguntas inéditas
tests/eval/paraphrases_v2.yaml conjunto congelado v2: 34 perguntas inéditas
docs/                      ADRs, avaliação, arquitetura do Desafio 2
demo/llm-cache/            cache auditado das conversas da demo (modo sem chave)
pyproject.toml             dependências diretas; requirements*.txt é o lock gerado
```

## Melhorias possíveis

- **Memória persistente** (Redis ou Postgres com prazo de expiração) para conversas que sobrevivem a reinícios e a mais de uma réplica da API; hoje a memória fica em RAM, por processo.
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
