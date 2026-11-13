"""Query graph for "Como cheguei a esta resposta": which tables and columns a SQL query touched.

Each table is a cluster, each column the query used is a node, a JOIN is an undirected edge,
filtered columns carry their condition, and grouped/aggregated/projected columns point to the
result node. Built from the final SQL with sqlglot (already a dependency) and rendered by
``st.graphviz_chart`` from a DOT string, which needs no extra package.

Best effort: columns are resolved against the real schema; anything that cannot be resolved
(CTE names, derived-table aliases, ambiguous unqualified names) is left out instead of guessed.
"""

from __future__ import annotations

import html
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

# Grafite e lavanda (BRAND.md v1.1). Graphviz ignores the Streamlit theme, so colors are set here.
CLUSTER_FILL, CLUSTER_LINE = "#F3F4F6", "#AEB5C0"
NODE_FILL, NODE_LINE, INK, INK_MUTED = "#FFFFFF", "#646C79", "#0E1520", "#465061"
FILTER_LINE, FILTER_TAG = "#C2410C", "#B23A0A"
RESULT_FILL = "#2F3A4A"
MAX_CONDITION = 48

AGGREGATE_LABEL: dict[type[exp.Expr], str] = {
    exp.Count: "conta",
    exp.Sum: "soma",
    exp.Avg: "média",
    exp.Min: "mínimo",
    exp.Max: "máximo",
}


@dataclass
class QueryGraph:
    tables: dict[str, list[str]] = field(default_factory=dict)  # table -> used columns, in order
    joins: list[tuple[str, str]] = field(default_factory=list)  # ("clientes.id", "compras.cliente_id")
    filters: dict[str, list[str]] = field(default_factory=dict)  # "compras.canal" -> ["canal = 'App'"]
    to_result: list[tuple[str, str]] = field(default_factory=list)  # ("clientes.estado", "agrupa")
    result_notes: list[str] = field(default_factory=list)  # "conta linhas", HAVING conditions

    def add_column(self, qualified: str) -> None:
        table, column = qualified.split(".", 1)
        columns = self.tables.setdefault(table, [])
        if column not in columns:
            columns.append(column)

    def link(self, qualified: str, label: str) -> None:
        if (qualified, label) not in self.to_result:
            self.to_result.append((qualified, label))


class _Resolver:
    def __init__(self, tree: exp.Expr, columns_by_table: Mapping[str, frozenset[str]]) -> None:
        known = {t.lower(): frozenset(c.lower() for c in cols) for t, cols in columns_by_table.items()}
        self.aliases: dict[str, str] = {}
        for table in tree.find_all(exp.Table):
            real = table.name.lower()
            if real in known:
                self.aliases[real] = real
                self.aliases[table.alias_or_name.lower()] = real
        self.columns = {t: known[t] for t in set(self.aliases.values())}
        self.select_aliases: dict[str, exp.Expr] = {}
        for select in tree.find_all(exp.Select):
            for projection in select.expressions:
                if isinstance(projection, exp.Alias):
                    self.select_aliases.setdefault(projection.alias.lower(), projection.this)

    def resolve(self, column: exp.Column) -> str | None:
        name = column.name.lower()
        if column.table:
            real = self.aliases.get(column.table.lower())
            return f"{real}.{name}" if real and name in self.columns[real] else None
        owners = [t for t, cols in self.columns.items() if name in cols]
        return f"{owners[0]}.{name}" if len(owners) == 1 else None

    def columns_of(self, node: exp.Expr, _seen: frozenset[str] = frozenset()) -> Iterator[str]:
        """Resolved columns under ``node``; a bare select alias (``GROUP BY mes``) expands to its expression."""
        found = [node] if isinstance(node, exp.Column) else list(node.find_all(exp.Column))
        for column in found:
            qualified = self.resolve(column)
            if qualified is not None:
                yield qualified
                continue
            alias = column.name.lower()
            if not column.table and alias in self.select_aliases and alias not in _seen:
                yield from self.columns_of(self.select_aliases[alias], _seen | {alias})


def _conjuncts(node: exp.Expr) -> Iterator[exp.Expr]:
    if isinstance(node, exp.And):
        yield from _conjuncts(node.left)
        yield from _conjuncts(node.right)
    elif isinstance(node, exp.Paren):
        yield from _conjuncts(node.this)
    else:
        yield node


def _unqualified_sql(node: exp.Expr) -> str:
    def strip(n: exp.Expr) -> exp.Expr:
        return exp.column(n.name) if isinstance(n, exp.Column) else n

    text = node.copy().transform(strip).sql(dialect="sqlite", normalize_functions="lower")
    return text if len(text) <= MAX_CONDITION else text[: MAX_CONDITION - 1] + "…"


def _join_pair(node: exp.Expr, resolver: _Resolver) -> tuple[str, str] | None:
    if not isinstance(node, exp.EQ):
        return None
    left, right = node.left, node.right
    if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
        return None
    a, b = resolver.resolve(left), resolver.resolve(right)
    if a is None or b is None or a.split(".")[0] == b.split(".")[0]:
        return None
    return a, b


def _aggregate_label(node: exp.AggFunc) -> str:
    label = AGGREGATE_LABEL.get(type(node), node.sql_name().lower())
    return f"{label} distintos" if isinstance(node.this, exp.Distinct) else label


def extract(sql: str, columns_by_table: Mapping[str, frozenset[str]]) -> QueryGraph | None:
    """``None`` when the SQL does not parse or touches no known table."""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except sqlglot.errors.SqlglotError:
        return None
    if tree is None:
        return None
    resolver = _Resolver(tree, columns_by_table)
    if not resolver.columns:
        return None
    graph = QueryGraph()
    for column in tree.find_all(exp.Column):
        qualified = resolver.resolve(column)
        if qualified is not None:
            graph.add_column(qualified)

    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        for condition in _conjuncts(on) if on is not None else ():
            pair = _join_pair(condition, resolver)
            if pair is not None and pair not in graph.joins:
                graph.joins.append(pair)

    for where in tree.find_all(exp.Where):
        for condition in _conjuncts(where.this):
            pair = _join_pair(condition, resolver)
            if pair is not None:  # implicit join: FROM a, b WHERE a.id = b.a_id
                if pair not in graph.joins:
                    graph.joins.append(pair)
                continue
            text = _unqualified_sql(condition)
            for qualified in dict.fromkeys(resolver.columns_of(condition)):
                graph.filters.setdefault(qualified, []).append(text)

    for having in tree.find_all(exp.Having):
        graph.result_notes.extend(f"após agrupar: {_unqualified_sql(c)}" for c in _conjuncts(having.this))

    for group in tree.find_all(exp.Group):
        for expression in group.expressions:
            for qualified in resolver.columns_of(expression):
                graph.link(qualified, "agrupa")

    aggregated: set[str] = set()
    for aggregate in tree.find_all(exp.AggFunc):
        label = _aggregate_label(aggregate)
        columns = list(dict.fromkeys(resolver.columns_of(aggregate)))
        if not columns and isinstance(aggregate.this, exp.Star) and "conta linhas" not in graph.result_notes:
            graph.result_notes.append("conta linhas")
        for qualified in columns:
            aggregated.add(qualified)
            graph.link(qualified, label)

    linked = {q for q, _ in graph.to_result}
    if isinstance(tree, exp.Select):
        for projection in tree.expressions:
            for qualified in resolver.columns_of(projection):
                if qualified not in linked and qualified not in aggregated:
                    graph.link(qualified, "mostra")
                    linked.add(qualified)
    return graph


def _q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _node_label(column: str, conditions: list[str]) -> str:
    if not conditions:
        return _q(column)
    tag = f'<FONT COLOR="{FILTER_TAG}" FACE="sans-serif" POINT-SIZE="9"><B>FILTRO</B></FONT>'
    lines = "<BR/>".join(f"{tag}  {html.escape(c, quote=False)}" for c in conditions)
    return f"<{lines}>"


def to_dot(graph: QueryGraph, row_count: int | None) -> str:
    lines = [
        "digraph consulta {",
        '  graph [rankdir=LR, bgcolor="transparent", nodesep=0.25, ranksep=0.6, pad=0.15];',
        f'  node [shape=box, style="rounded,filled", fillcolor="{NODE_FILL}", color="{NODE_LINE}", '
        f'fontcolor="{INK}", fontname="monospace", fontsize=11, height=0.32, margin="0.14,0.06"];',
        f'  edge [color="{INK}", fontcolor="{INK_MUTED}", fontname="sans-serif", fontsize=10, arrowsize=0.6];',
    ]
    for table, columns in graph.tables.items():
        lines.append(f"  subgraph {_q('cluster_' + table)} {{")
        lines.append(
            f'    label={_q(table)}; labeljust=l; style="rounded,filled"; fillcolor="{CLUSTER_FILL}"; '
            f'color="{CLUSTER_LINE}"; fontcolor="{INK_MUTED}"; fontname="monospace"; fontsize=11;'
        )
        for column in columns:
            qualified = f"{table}.{column}"
            conditions = graph.filters.get(qualified, [])
            if conditions:
                label = _node_label(column, conditions)
                attrs = f'label={label}, color="{FILTER_LINE}", style="rounded,filled,dashed", penwidth=1.5'
            else:
                attrs = f"label={_q(column)}"
            lines.append(f"    {_q(qualified)} [{attrs}];")
        lines.append("  }")
    for a, b in graph.joins:
        lines.append(f'  {_q(a)} -> {_q(b)} [dir=none, label="JOIN", penwidth=1.5];')
    result = "resultado" if row_count is None else f"resultado · {row_count} {'linha' if row_count == 1 else 'linhas'}"
    label = "\\n".join([result, *graph.result_notes])
    lines.append(
        f'  "__resultado__" [label="{label}", fillcolor="{RESULT_FILL}", color="{RESULT_FILL}", '
        'fontcolor="#FFFFFF", fontname="sans-serif"];'
    )
    for qualified, edge_label in graph.to_result:
        lines.append(f'  {_q(qualified)} -> "__resultado__" [label={_q(edge_label)}];')
    for i, qualified in enumerate(graph.filters):
        label = ', label="filtra"' if i == 0 else ""
        lines.append(f'  {_q(qualified)} -> "__resultado__" [style=dashed, color="{FILTER_LINE}"{label}];')
    lines.append("}")
    return "\n".join(lines)
