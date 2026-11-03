# Disposable hosted validation image; synthetic tools stay outside the application wheel.
ARG UV_DIGEST=sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b
ARG PYTHON_DIGEST=sha256:a70519002c49552ea0a853de47599cf40479b001bd7a624f1112eaf44dcaccc7
FROM ghcr.io/astral-sh/uv:0.13.4@${UV_DIGEST} AS uv
FROM python:3.14-slim-bookworm@${PYTHON_DIGEST} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project \
    --extra postgres --extra redis --extra foundry-model --extra azure-runtime
ENV TIKTOKEN_CACHE_DIR=/app/token-cache
RUN uv run --no-sync python -c \
    "import tiktoken; tiktoken.encoding_for_model('gpt-4o-2024-11-20')"
COPY src/ src/
COPY prompts/ prompts/
RUN uv sync --locked --no-dev --no-editable \
    --extra postgres --extra redis --extra foundry-model --extra azure-runtime

FROM python:3.14-slim-bookworm@${PYTHON_DIGEST}
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
ENV TIKTOKEN_CACHE_DIR=/app/token-cache
COPY --from=build /app/token-cache /app/token-cache
COPY --from=build /app/.venv /app/.venv
COPY tests/__init__.py tests/__init__.py
COPY tests/validation/ tests/validation/
COPY tests/azure/ tests/azure/
USER 10001:10001
EXPOSE 8088
ENTRYPOINT ["python", "-m", "tests.azure.pilot"]
