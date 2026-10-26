# syntax=docker/dockerfile:1
#
# The vessel-mind image. compose.yaml builds it from the repository root.
#
# The build context must be a full clone (not shallow, not a source archive):
# the index stage reads the vessel-mind corpus source at its pinned commit from
# .git (eval/corpus.yaml, fetch: git-archive). Only that stage sees .git, through
# a read-only bind mount, so no layer of any image holds the history.
#
# Stages:
#   notion-mcp   installs the pinned Notion MCP server with npm ci (never npx)
#   python-deps  installs the locked Python dependencies with uv
#   index        ingests the pinned corpus: data/index.sqlite, the embedding model
#                and the reranker model
#   runtime      the app; it starts with a complete index and embeds nothing

FROM node:22.23.4-trixie-slim@sha256:8eb619e402eaede1748a3c8173bffab391592812e85136452f781741c7242ddf AS notion-mcp
WORKDIR /build/deploy/notion-mcp
COPY deploy/notion-mcp/package.json deploy/notion-mcp/package-lock.json ./
# Exactly the versions in the committed lockfile; no install scripts run.
RUN npm ci --ignore-scripts --omit=dev

FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS python-deps
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python-deps AS index
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY src/ src/
COPY eval/corpus.yaml eval/corpus.yaml
# The cpu reranker (retrieval.reranker) downloads its model on the first search;
# score once here so the model is in .cache/fastembed, the runtime's
# retrieval.model_cache. It uses the default model, as the example configs do.
RUN PYTHONPATH=src /app/.venv/bin/python -c "from pathlib import Path; \
from vessel_mind.rag.rerank import FastEmbedCrossEncoder; \
FastEmbedCrossEncoder(cache_dir=Path('.cache/fastembed')).score('warm up', ['download the model'])"
# The vessel-mind source comes from .git at its pin; the VibeConform tarball is
# downloaded and checked against its SHA-256. The embedding model is downloaded
# into .cache/fastembed, the runtime's retrieval.model_cache.
RUN --mount=type=bind,source=.git,target=/repo/.git \
    git config --global --add safe.directory /repo \
    && PYTHONPATH=src /app/.venv/bin/python -m vessel_mind.rag.ingest \
        --corpus eval/corpus.yaml \
        --repo-root /repo \
        --index data/index.sqlite \
        --cache-dir .cache/corpus \
        --model-cache-dir .cache/fastembed

FROM python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS runtime
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --no-create-home app
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1
# node alone starts the pinned Notion MCP server over stdio; npm is not copied.
COPY --from=notion-mcp /usr/local/bin/node /usr/local/bin/node
COPY --from=notion-mcp /build/deploy/notion-mcp/node_modules deploy/notion-mcp/node_modules
COPY deploy/notion-mcp/package.json deploy/notion-mcp/package-lock.json deploy/notion-mcp/
COPY --from=python-deps /app/.venv /app/.venv
COPY src/ src/
COPY config/ config/
COPY eval/corpus.yaml eval/corpus.yaml
# The finished index, the model that embeds queries against it and the
# reranker's model (both in .cache/fastembed). The state
# file goes to data/state, a volume in compose.yaml.
COPY --from=index --chown=app:app /app/data/index.sqlite data/index.sqlite
COPY --from=index --chown=app:app /app/.cache/fastembed .cache/fastembed
RUN mkdir -p data/state && chown app:app data data/state
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]
# 0.0.0.0 inside the container; compose.yaml publishes the port on 127.0.0.1 only.
CMD ["uvicorn", "--factory", "vessel_mind.server.main:app_from_config", "--host", "0.0.0.0", "--port", "8000"]
