# ADR 0002 — SQLite relacional, sem banco de grafos

- **Status:** aceito (2026-11-12)

## Contexto
O desafio fornece um SQLite com 4 tabelas e cerca de 1.600 linhas, ligadas por uma única chave (`cliente_id`). As perguntas são agregações (contagens, médias, séries mensais).

## Decisão
Consultar o SQLite diretamente. Não usar Neo4j nem outro banco de grafos.

## Consequências
- Zero infraestrutura extra: o avaliador sobe tudo com uma chave de API.
- As perguntas do enunciado são agregações relacionais, para as quais o SQL é a linguagem natural do problema.

## Quando reconsiderar
Se o domínio passar a ter relações de vários saltos (indicações entre clientes, jornadas multicanal encadeadas) ou se for preciso um catálogo semântico navegável. Uma "visão em grafo" gerada a partir do próprio SQLite fica como extensão no README.

## Atualização (2026-11-12): grafo da consulta

A visão em grafo foi implementada sem banco de grafos e sem dependência nova. O painel "Como cheguei a esta resposta" desenha o **grafo da consulta**: cada tabela é um grupo, cada coluna usada é um nó, cada `JOIN` é uma aresta, e filtros e agrupamentos ficam marcados. O grafo é extraído da SQL final com `sqlglot` (`src/ui/query_graph.py`) e desenhado com `st.graphviz_chart` a partir de uma string DOT. Ele explica cada resposta para quem não lê SQL. Um grafo dos registros de clientes foi descartado: ficaria emaranhado e exporia nome e e-mail.
