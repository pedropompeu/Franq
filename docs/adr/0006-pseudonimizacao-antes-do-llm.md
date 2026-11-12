# ADR 0006 — Pseudonimização de nome e e-mail antes do LLM

- **Status:** aceito (2026-11-12)

## Contexto

A tabela `clientes` tem nome e e-mail (dados pessoais na LGPD, ainda que fictícios aqui). O Gemini é um serviço externo, e o cache local guardaria as respostas dele.

## Decisão

`src/privacy.py` troca cada nome e e-mail por um token estável (`[nome_3]`, `[email_3]`, mesmo número para o mesmo cliente) em **tudo** o que vai ao modelo: contexto, pergunta, linhas de resultado, SQL e erros do histórico. As colunas vêm do dicionário (`pii: true`).

- A SQL do modelo é desmascarada (com aspas escapadas) antes de executar, então roda nos dados reais.
- Resposta, premissas e raciocínio são desmascarados antes de chegar à tela; a tabela e o CSV mostram os valores reais.
- O trace grava a pergunta e a SQL mascaradas e nunca guarda linhas de resultado.
- Um prompt sem dado pessoal fica idêntico ao original, então o cache continua válido.

## Alternativas descartadas

- **Mascarar também a tela e o CSV**: o usuário autorizado veria dados cortados.
- **Só um aviso na interface**: o Gemini continuaria recebendo os dados.

## Consequências

- Verificado com o modelo real: a avaliação conta as mensagens com valor real enviadas ao LLM (resultado: 0 em 48 execuções).
- Limite: o que o usuário digita só é mascarado se bater com um valor do banco. Um nome com erro de digitação segue como texto.
