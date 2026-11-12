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
