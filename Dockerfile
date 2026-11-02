# Pin build tools and Python to reviewed image digests; dependencies use uv.lock.
ARG UV_DIGEST=sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b
ARG PYTHON_DIGEST=sha256:a70519002c49552ea0a853de47599cf40479b001bd7a624f1112eaf44dcaccc7
FROM ghcr.io/astral-sh/uv:0.13.4@${UV_DIGEST} AS uv
FROM python:3.14-slim-bookworm@${PYTHON_DIGEST} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra foundry-hosting --extra demo --no-install-project
COPY src/ src/
COPY prompts/ prompts/
RUN uv sync --locked --no-dev --extra foundry-hosting --extra demo --no-editable

FROM python:3.14-slim-bookworm@${PYTHON_DIGEST} AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
USER 10001:10001
EXPOSE 8088
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", \
         "import urllib.request; \
urllib.request.urlopen('http://127.0.0.1:8088/readiness', timeout=2).close()"]
# Direct execution makes the server PID 1 so docker stop reaches its signal handler.
ENTRYPOINT ["bankeriq-runtime"]
CMD ["serve", "--provider", "mock", "--host", "0.0.0.0", "--port", "8088"]
