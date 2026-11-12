"""Data graph for the "Visão em grafo" tab: how states, categories, channels and campaigns connect.

Built straight from SQLite, without Neo4j and without the LLM. Each node is a value of a business
dimension (a state, a purchase category, a support channel...) sized by the number of distinct
clients it has; each edge links two values of different dimensions and is weighted by the number
of clients that have both. Only aggregates leave this module: client ids are used to intersect
sets and never reach the page, and the personal columns (``nome``, ``email``) are never read.

Rendered by pyvis (vis-network) with the JavaScript inlined, so the page works offline.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from src.db.tools import open_readonly
from src.ui.view import CATEGORICAL

# Grafite e laranja (BRAND.md). vis-network ignores the Streamlit theme, so colors are set here.
SURFACE, INK, INK_MUTED, EDGE = "#FFFFFF", "#0E1520", "#465061", "#AEB5C0"
HEIGHT_PX = 620
VIS_OPTIONS = {
    "nodes": {
        "font": {"size": 15, "face": "sans-serif", "strokeWidth": 3, "strokeColor": SURFACE},
        "borderWidth": 2,
        "scaling": {"min": 8, "max": 26},
    },
    "edges": {
        "color": {"color": EDGE, "highlight": INK, "opacity": 0.75},
        "smooth": False,
        "scaling": {"min": 1, "max": 9},
    },
    "interaction": {"hover": True, "tooltipDelay": 120},
    "physics": {
        "solver": "forceAtlas2Based",
        "forceAtlas2Based": {"gravitationalConstant": -120, "springLength": 170, "avoidOverlap": 0.8},
        "stabilization": {"iterations": 250},
    },
}


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    table: str
    column: str
    client_column: str = "cliente_id"
    where: str = ""  # fixed filter, written here (never user input)
    filter_column: str | None = None
    shape: str = "dot"  # second encoding besides color, so identity never relies on color alone
    symbol: str = "●"

    @property
    def required(self) -> frozenset[str]:
        return frozenset(c for c in (self.client_column, self.column, self.filter_column) if c)

    def sql(self) -> str:
        where = f"{self.column} IS NOT NULL" + (f" AND {self.where}" if self.where else "")
        return f"SELECT DISTINCT {self.client_column}, {self.column} FROM {self.table} WHERE {where}"


DIMENSIONS: tuple[Dimension, ...] = (
    Dimension("estado", "Estado", "clientes", "estado", client_column="id", shape="dot", symbol="●"),
    Dimension("categoria", "Categoria de compra", "compras", "categoria", shape="square", symbol="■"),
    Dimension("canal_compra", "Canal de compra", "compras", "canal", shape="diamond", symbol="◆"),
    Dimension("canal_suporte", "Canal de suporte", "suporte", "canal", shape="triangle", symbol="▲"),
    Dimension(
        "campanha",
        "Campanha (interagiu)",
        "campanhas_marketing",
        "nome_campanha",
        where="interagiu = 1",
        filter_column="interagiu",
        shape="star",
        symbol="★",
    ),
)
DEFAULT_SELECTION: tuple[str, ...] = ("estado", "categoria")


def color(dim: Dimension) -> str:
    return CATEGORICAL[DIMENSIONS.index(dim) % len(CATEGORICAL)]


def available(columns_by_table: Mapping[str, frozenset[str]]) -> tuple[Dimension, ...]:
    """Dimensions whose table and columns exist in the real schema (the graph adapts instead of failing)."""
    return tuple(d for d in DIMENSIONS if d.required <= columns_by_table.get(d.table, frozenset()))


Memberships = dict[str, dict[str, frozenset[int]]]  # dimension key -> value -> client ids


def load_memberships(db_path: Path, dims: Sequence[Dimension]) -> Memberships:
    """Which clients have each value, through the same read-only connection as ``run_sql``."""
    out: Memberships = {}
    with closing(open_readonly(db_path)) as conn:
        for dim in dims:
            groups: dict[str, set[int]] = {}
            for client, value in conn.execute(dim.sql()):
                groups.setdefault(str(value), set()).add(int(client))
            out[dim.key] = {value: frozenset(ids) for value, ids in groups.items()}
    return out


@dataclass(frozen=True)
class Node:
    id: str
    dim: Dimension
    value: str
    clients: int


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    clients: int


@dataclass(frozen=True)
class DataGraph:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]

    @property
    def max_edge(self) -> int:
        return max((e.clients for e in self.edges), default=0)


def node_id(dim: Dimension, value: str) -> str:
    return f"{dim.key}:{value}"


def build(memberships: Memberships, dims: Sequence[Dimension], min_clients: int = 1) -> DataGraph:
    """Nodes for every value of the chosen dimensions; edges between dimensions with ``>= min_clients``."""
    nodes = [
        Node(node_id(dim, value), dim, value, len(ids))
        for dim in dims
        for value, ids in sorted(memberships.get(dim.key, {}).items())
    ]
    edges: list[Edge] = []
    for left, right in combinations(dims, 2):
        for a_value, a_ids in sorted(memberships.get(left.key, {}).items()):
            for b_value, b_ids in sorted(memberships.get(right.key, {}).items()):
                shared = len(a_ids & b_ids)
                if shared >= max(min_clients, 1):
                    edges.append(Edge(node_id(left, a_value), node_id(right, b_value), shared))
    return DataGraph(tuple(nodes), tuple(edges))


def strongest(graph: DataGraph, limit: int = 10) -> list[dict[str, str | int]]:
    """The heaviest links as table rows: the accessible, exportable view of the same graph."""
    names = {n.id: n for n in graph.nodes}
    ranked = sorted(graph.edges, key=lambda e: (-e.clients, e.source, e.target))[:limit]
    return [
        {
            "De": f"{names[e.source].value} ({names[e.source].dim.label})",
            "Para": f"{names[e.target].value} ({names[e.target].dim.label})",
            "Clientes em comum": e.clients,
        }
        for e in ranked
    ]


def _text(value: str) -> str:
    # Values come from the database and end up inside a <script>; angle brackets cannot close it.
    return value.replace("<", "\u2039").replace(">", "\u203a")  # ‹ ›  # noqa: RUF003


_CDN_TAG = re.compile(r"<(?:link|script)\b[^>]*https?://[^>]*>(?:\s*</script>)?", re.IGNORECASE)
_EMPTY_HEADING = re.compile(r"<center>\s*<h1>\s*</h1>\s*</center>", re.IGNORECASE)


def _legend(dims: Sequence[Dimension]) -> str:
    items = "".join(
        f'<span style="margin-right:16px;white-space:nowrap">'
        f'<span style="color:{color(d)};font-size:16px">{d.symbol}</span> {d.label}</span>'
        for d in dims
    )
    return (
        f'<div style="font:14px sans-serif;color:{INK};padding:4px 0 8px">{items}'
        f'<span style="color:{INK_MUTED}">· tamanho = clientes · espessura da linha = clientes em comum</span></div>'
    )


def to_html(graph: DataGraph, dims: Sequence[Dimension]) -> str:
    """Self-contained vis-network page: inline JS, no CDN, no web fonts."""
    from pyvis.network import Network  # type: ignore[import-untyped]  # no stubs upstream

    net = Network(height=f"{HEIGHT_PX}px", width="100%", bgcolor=SURFACE, font_color=INK, cdn_resources="in_line")
    net.set_options(json.dumps(VIS_OPTIONS))
    names = {n.id: n for n in graph.nodes}
    for node in graph.nodes:
        clients = "1 cliente" if node.clients == 1 else f"{node.clients} clientes"
        net.add_node(
            node.id,
            label=_text(node.value),
            title=_text(f"{node.dim.label}: {node.value} · {clients}"),
            color={"background": color(node.dim), "border": SURFACE},
            shape=node.dim.shape,
            value=node.clients,
        )
    for edge in graph.edges:
        a, b = names[edge.source], names[edge.target]
        shared = "1 cliente" if edge.clients == 1 else f"{edge.clients} clientes"
        net.add_edge(edge.source, edge.target, value=edge.clients, title=_text(f"{a.value} ↔ {b.value}: {shared}"))
    page = net.generate_html(notebook=False)
    page = _CDN_TAG.sub("", page)  # pyvis always adds Bootstrap from a CDN; the graph does not use it
    page = _EMPTY_HEADING.sub("", page)
    page = page.replace("border: 1px solid lightgray;", "border: 0;")
    return page.replace("<body>", f'<body style="margin:0;background:{SURFACE}">{_legend(dims)}', 1)
