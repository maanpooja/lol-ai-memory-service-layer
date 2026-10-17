# memory-mcp server image for Cloud Run. Built by `gcloud run deploy --source .` (Cloud Build).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies first, so code changes reuse this layer. Dev and agent groups are left out.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

RUN useradd --system --no-create-home app
USER app
ENV PATH="/app/.venv/bin:$PATH"

# Cloud Run sets PORT. stdout and stderr go to Cloud Logging; audit lines are JSON on stderr.
CMD ["sh", "-c", "exec memory-mcp --transport http --host 0.0.0.0 --port ${PORT:-8080}"]
