"""Data graph ("Visão em grafo"): aggregates only, real counts, no personal data, no CDN."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from src.ui import data_graph as dg

ALL_COLUMNS = {
    "clientes": frozenset({"id", "nome", "email", "estado"}),
    "compras": frozenset({"cliente_id", "categoria", "canal"}),
    "suporte": frozenset({"cliente_id", "canal"}),
    "campanhas_marketing": frozenset({"cliente_id", "nome_campanha", "interagiu"}),
}
BY_KEY = {d.key: d for d in dg.DIMENSIONS}


@pytest.fixture(scope="module")
def memberships(db_path: Path) -> dg.Memberships:
    return dg.load_memberships(db_path, dg.DIMENSIONS)


def _scalar(db_path: Path, sql: str) -> int:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as con:
        return int(con.execute(sql).fetchone()[0])


def test_dimensions_follow_the_real_schema() -> None:
    assert dg.available(ALL_COLUMNS) == dg.DIMENSIONS
    partial = {**ALL_COLUMNS, "campanhas_marketing": frozenset({"cliente_id", "nome_campanha"})}
    assert "campanha" not in {d.key for d in dg.available(partial)}
    assert {d.key for d in dg.available({"clientes": ALL_COLUMNS["clientes"]})} == {"estado"}


def test_personal_columns_are_never_read() -> None:
    for dim in dg.DIMENSIONS:
        assert not re.search(r"\b(nome|email)\b", dim.sql())


def test_node_size_is_the_number_of_distinct_clients(db_path: Path, memberships: dg.Memberships) -> None:
    assert sum(len(ids) for ids in memberships["estado"].values()) == _scalar(db_path, "SELECT COUNT(*) FROM clientes")
    interacted = _scalar(db_path, "SELECT COUNT(DISTINCT cliente_id) FROM campanhas_marketing WHERE interagiu = 1")
    assert len(frozenset().union(*memberships["campanha"].values())) == interacted
    black_friday = _scalar(
        db_path,
        "SELECT COUNT(DISTINCT cliente_id) FROM campanhas_marketing "
        "WHERE nome_campanha = 'Black Friday' AND interagiu = 1",
    )
    assert len(memberships["campanha"]["Black Friday"]) == black_friday


def test_edge_weight_matches_sql(db_path: Path, memberships: dg.Memberships) -> None:
    dims = [BY_KEY["estado"], BY_KEY["categoria"]]
    graph = dg.build(memberships, dims)
    edge = next(e for e in graph.edges if e.source == "estado:São Paulo" and e.target == "categoria:Livros")
    expected = _scalar(
        db_path,
        "SELECT COUNT(DISTINCT c.id) FROM clientes c JOIN compras p ON p.cliente_id = c.id "
        "WHERE c.estado = 'São Paulo' AND p.categoria = 'Livros'",
    )
    assert edge.clients == expected > 0


def test_edges_only_link_different_dimensions_and_respect_the_minimum(memberships: dg.Memberships) -> None:
    dims = [BY_KEY["estado"], BY_KEY["categoria"], BY_KEY["canal_suporte"]]
    every = dg.build(memberships, dims, min_clients=1)
    strong = dg.build(memberships, dims, min_clients=5)
    assert all(e.source.split(":")[0] != e.target.split(":")[0] for e in every.edges)
    assert all(e.clients >= 5 for e in strong.edges) and 0 < len(strong.edges) < len(every.edges)
    assert len(strong.nodes) == len(every.nodes)  # filtering edges never hides a value


def test_strongest_lists_heaviest_links_first(memberships: dg.Memberships) -> None:
    graph = dg.build(memberships, [BY_KEY["estado"], BY_KEY["categoria"]])
    rows = dg.strongest(graph, limit=3)
    weights = [int(r["Clientes em comum"]) for r in rows]
    assert len(rows) == 3 and weights == sorted(weights, reverse=True) and weights[0] == graph.max_edge


def test_html_is_self_contained_and_has_no_personal_data(db_path: Path, memberships: dg.Memberships) -> None:
    dims = [BY_KEY["estado"], BY_KEY["categoria"]]
    page = dg.to_html(dg.build(memberships, dims, min_clients=3), dims)
    assert not re.search(r"<(?:script|link)\b[^>]*\b(?:src|href)=[\"']?https?://", page, re.IGNORECASE)
    assert "vis.Network" in page and "Estado" in page and "Categoria de compra" in page
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as con:
        people = [value for row in con.execute("SELECT nome, email FROM clientes") for value in row]
    assert not any(value in page for value in people)


def test_database_values_cannot_close_the_script_tag() -> None:
    evil = "</script><script>alert(1)</script>"
    dims = [BY_KEY["estado"], BY_KEY["categoria"]]
    memberships: dg.Memberships = {"estado": {evil: frozenset({1})}, "categoria": {"Livros": frozenset({1})}}
    page = dg.to_html(dg.build(memberships, dims), dims)
    assert evil not in page and "alert(1)" in page  # still shown, as inert text
