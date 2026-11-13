"""Interface copy (pt-BR). Voice: precise, transparent, sober. Number first, assumption right after.

Kept in one module so the tone is reviewed in one place (see BRAND.md, section "Voz").
"""

from __future__ import annotations

from typing import Final

APP_TITLE: Final = "Indaga"
SUBTITLE: Final = "Assistente de dados"
TAGLINE: Final = "Pergunte. Veja a consulta. Confie no número."
INPUT_PLACEHOLDER: Final = "Pergunte sobre clientes, compras, suporte ou campanhas"
WELCOME: Final = (
    "Olá! Sou o Indaga, seu assistente de dados. Pergunte em português sobre clientes, compras, "
    "suporte e campanhas: eu monto a consulta, trago o resultado e mostro como cheguei nele."
)
WELCOME_COVERAGE: Final = (
    "Os dados vão de **{start}** a **{end}**. Quando você disser “maio” ou “último ano”, "
    "conto a partir do fim desse período."
)
WELCOME_SUGGESTIONS: Final = "Sugestões de perguntas:"
# Live progress while the answer is built (src/ui/progress_log.py). "doing" is the status label;
# "done" goes to the list of finished stages when the next stage starts.
PROGRESS_START: Final = "Preparando a resposta…"
PROGRESS_HINT: Final = "Cada etapa consulta o modelo de linguagem e costuma levar alguns segundos."
PROGRESS_DOING: Final = {
    "context": "Lendo o esquema do banco…",
    "planning": "Entendendo a pergunta e escrevendo a consulta…",
    "reviewing": "Conferindo o resultado e montando a resposta…",
    "fixing": "Corrigindo a consulta…",
}
PROGRESS_DONE: Final = {
    "context": "Esquema e regras de negócio carregados",
    "planning": "Pergunta analisada",
    "reviewing": "Resultado conferido",
    "fixing": "Consulta reescrita",
}
PROGRESS_QUERY_DONE: Final = "Consulta executada no banco: {rows} em {ms} ms"
PROGRESS_QUERY_FAILED: Final = "O banco recusou a consulta; vou corrigir"
PROGRESS_WAIT: Final = {
    "rate_limit": "Limite de pedidos por minuto do Gemini: aguardando cerca de {seconds} s…",
    "retry": "O Gemini não respondeu; nova tentativa em cerca de {seconds} s…",
}
PROGRESS_WAITED: Final = {
    "rate_limit": "Aguardou o limite por minuto do Gemini (cerca de {seconds} s)",
    "retry": "Nova tentativa após falha do Gemini (cerca de {seconds} s)",
}
PROGRESS_FALLBACK: Final = "Modelo principal indisponível; seguindo com {model}"

# The five questions from the challenge statement, sent as written; the button shows the short form.
SUGGESTED_QUESTIONS: Final = (
    "Liste os 5 estados com maior número de clientes que compraram via app em maio.",
    "Quantos clientes interagiram com campanhas de WhatsApp em 2024?",
    "Quais categorias de produto tiveram o maior número de compras em média por cliente?",
    "Qual o número de reclamações não resolvidas por canal?",
    "Qual a tendência de reclamações por canal no último ano?",
)
SUGGESTION_LABELS: Final = (
    ("Ranking", "Estados com mais clientes no app em maio"),
    ("Contagem", "Clientes em campanhas de WhatsApp em 2024"),
    ("Média", "Categorias com mais compras por cliente"),
    ("Por canal", "Reclamações não resolvidas por canal"),
    ("Tendência", "Reclamações por canal no último ano"),
)

STATUS_LABEL: Final = {"answered": "Respondida", "refused": "Recusada", "failed": "Sem resposta"}

STEP_LABEL: Final = {
    "query": "Consulta",
    "query_error": "Consulta com erro",
    "answer": "Resposta",
    "refusal": "Recusa",
    "llm_error": "Falha do modelo",
    "limit": "Limite atingido",
}

ASSUMPTION: Final = "Premissa"
REASONING: Final = "Como cheguei a esta resposta"
GRAPH_CAPTION: Final = (
    "Cada caixa clara é uma tabela; cada nó, uma coluna que a consulta usou. A linha sem seta liga as "
    "colunas do JOIN, o contorno laranja tracejado marca filtro e o bloco escuro é o resultado."
)
VIEW_CHART: Final = "Gráfico"
VIEW_TABLE: Final = "Tabela"
DOWNLOAD_CSV: Final = "Baixar CSV"
NO_ROWS: Final = "A consulta não retornou linhas."

MISSING_KEY: Final = (
    "Sem acesso ao modelo: {error}\n\nPara usar só respostas já guardadas, rode com `LLM_CACHE=replay`."
)

QUALITY_TRUNCATED: Final = "Resultado limitado a {rows} linhas. A exportação também está limitada."
QUALITY_PII: Final = "Contém dados pessoais ({columns}). Compartilhe apenas com quem precisa."
QUALITY_CORRECTED: Final = (
    "A primeira consulta falhou; o agente corrigiu sozinho (correções: {n}). Veja “Como cheguei a esta resposta”."
)
QUALITY_UNRELIABLE: Final = "A consulta usa coluna marcada como não confiável ({columns}). Confira o número."

SIDEBAR_ABOUT: Final = "Sobre os dados"
SIDEBAR_UNRELIABLE: Final = "Colunas ignoradas por divergência com a tabela de origem: {columns}."
SIDEBAR_SCHEMA_WARNINGS: Final = "Dicionário de dados fora do schema"
SIDEBAR_MODEL: Final = "Modelo"
SIDEBAR_CACHE_MODE: Final = {
    "on": "Cache local ligado: perguntas repetidas não gastam cota.",
    "off": "Cache local desligado.",
    "replay": "Modo replay: só respostas já guardadas, sem chamar a API.",
}

TAB_ASK: Final = "Perguntar"
TAB_GRAPH: Final = "Visão em grafo"
TAB_TRACES: Final = "Rastros"
GRAPH_INTRO: Final = (
    "Como estados, compras, suporte e campanhas se ligam pelos mesmos clientes. Cada ligação conta os "
    "clientes que têm as duas coisas, não necessariamente na mesma compra ou no mesmo contato. "
    "Arraste os nós, use a roda do mouse para o zoom e passe o mouse para ver os números."
)
GRAPH_DIMENSIONS: Final = "Mostrar"
GRAPH_MIN_CLIENTS: Final = "Mínimo de clientes por ligação"
GRAPH_PICK_TWO: Final = "Escolha pelo menos duas dimensões para ver as ligações."
GRAPH_SUMMARY: Final = "{nodes} nós · {edges} ligações · nenhum dado pessoal: só contagens de clientes."
GRAPH_TABLE: Final = "Ligações mais fortes"
GRAPH_EMPTY: Final = "Nenhuma ligação com esse mínimo. Diminua o mínimo de clientes."
TRACES_EMPTY: Final = "Nenhuma pergunta registrada ainda."
TRACES_CAPTION: Final = (
    "Um registro por pergunta em `{path}`: passos, SQL, erros, tokens e latência. Sem linhas de resultado."
)
