# Imagem de execução do Assistente Virtual de Dados (Streamlit na porta 8501).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Só o que a aplicação precisa para rodar (ver .dockerignore): sem testes, sem .env, sem cache.
COPY app.py .
COPY .streamlit/ .streamlit/
COPY src/ src/
COPY data/ data/
# Cache auditado das conversas da demo: LLM_CACHE=replay + LLM_CACHE_DIR=/app/demo/llm-cache, sem chave.
COPY demo/ demo/

# Usuário sem privilégios; só .cache (respostas do LLM) e traces/ são graváveis.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p .cache traces \
    && chown app:app .cache traces
USER app

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4)"

CMD ["python", "-m", "streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
