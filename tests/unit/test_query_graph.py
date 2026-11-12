"""Query graph: tables as clusters, used columns as nodes, JOINs as edges, filters marked."""

from __future__ import annotations

import pytest

from src.ui import query_graph as qg

COLUMNS = {
    "clientes": frozenset({"id", "nome", "email", "estado"}),
    "compras": frozenset({"id", "cliente_id", "canal", "categoria", "data_compra"}),
    "suporte": frozenset({"id", "cliente_id", "canal", "tipo_contato", "resolvido", "data_contato"}),
}

P1 = (
    "SELECT c.estado AS estado, COUNT(DISTINCT c.id) AS numero_de_clientes FROM clientes c "
    "JOIN compras cp ON c.id = cp.cliente_id WHERE cp.canal = 'App' "
    "AND strftime('%Y-%m', cp.data_compra) = '2025-05' GROUP BY c.estado "
    "ORDER BY numero_de_clientes DESC, c.estado ASC LIMIT 5"
)


def test_join_query_becomes_two_clusters_linked_by_the_join() -> None:
    graph = qg.extract(P1, COLUMNS)
    assert graph is not None
    assert graph.tables == {"clientes": ["estado", "id"], "compras": ["cliente_id", "canal", "data_compra"]}
    assert graph.joins == [("clientes.id", "compras.cliente_id")]
    assert graph.filters == {
        "compras.canal": ["canal = 'App'"],
        "compras.data_compra": ["strftime('%Y-%m', data_compra) = '2025-05'"],
    }
    assert graph.to_result == [("clientes.estado", "agrupa"), ("clientes.id", "conta distintos")]


def test_group_by_alias_expands_to_the_source_column() -> None:
    sql = (
        "SELECT strftime('%Y-%m', data_contato) AS mes, canal, COUNT(*) AS total FROM suporte "
        "WHERE tipo_contato = 'Reclamação' GROUP BY mes, canal ORDER BY mes, canal"
    )
    graph = qg.extract(sql, COLUMNS)
    assert graph is not None
    assert graph.to_result == [("suporte.data_contato", "agrupa"), ("suporte.canal", "agrupa")]
    assert graph.result_notes == ["conta linhas"]


def test_implicit_join_in_where_is_an_edge_not_a_filter() -> None:
    sql = "SELECT c.nome FROM clientes c, suporte s WHERE c.id = s.cliente_id AND s.resolvido = 0"
    graph = qg.extract(sql, COLUMNS)
    assert graph is not None
    assert graph.joins == [("clientes.id", "suporte.cliente_id")]
    assert list(graph.filters) == ["suporte.resolvido"]
    assert ("clientes.nome", "mostra") in graph.to_result


def test_having_goes_to_the_result_node() -> None:
    graph = qg.extract("SELECT canal, COUNT(*) FROM compras GROUP BY canal HAVING COUNT(*) > 10", COLUMNS)
    assert graph is not None
    assert "após agrupar: count(*) > 10" in graph.result_notes


def test_unknown_names_are_left_out_instead_of_guessed() -> None:
    sql = "WITH t AS (SELECT cliente_id, COUNT(*) AS n FROM compras GROUP BY cliente_id) SELECT AVG(n) FROM t"
    graph = qg.extract(sql, COLUMNS)
    assert graph is not None
    assert graph.tables == {"compras": ["cliente_id"]}
    # "id" exists in both tables: ambiguous, so it is not drawn
    ambiguous = qg.extract("SELECT id FROM clientes JOIN compras ON clientes.id = compras.cliente_id", COLUMNS)
    assert ambiguous is not None and ("clientes.id", "mostra") not in ambiguous.to_result


@pytest.mark.parametrize("sql", ["SELEC nada", "SELECT 1", "SELECT * FROM tabela_inexistente"])
def test_no_graph_when_nothing_is_known(sql: str) -> None:
    assert qg.extract(sql, COLUMNS) is None


def test_dot_marks_filters_and_escapes_text() -> None:
    graph = qg.extract("SELECT canal FROM compras WHERE categoria = 'A & \"B\" <C>'", COLUMNS)
    assert graph is not None
    dot = qg.to_dot(graph, 1)
    assert dot.startswith("digraph consulta {") and dot.endswith("}")
    assert 'subgraph "cluster_compras"' in dot
    assert "FILTRO" in dot and "&amp;" in dot and "&lt;C&gt;" in dot
    assert "resultado · 1 linha" in dot
    assert 'style=dashed, color="#C2410C", label="filtra"' in dot


def test_dot_join_edge_is_undirected() -> None:
    graph = qg.extract(P1, COLUMNS)
    assert graph is not None
    dot = qg.to_dot(graph, 5)
    assert '"clientes.id" -> "compras.cliente_id" [dir=none, label="JOIN"' in dot
    assert "resultado · 5 linhas" in dot
