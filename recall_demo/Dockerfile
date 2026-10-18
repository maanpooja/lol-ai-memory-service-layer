# Recall demo web app image. Built from the repo root:
#   gcloud builds submit --config deploy/recall-demo-cloudbuild.yaml .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Server package (for the shared OIDC verifier) plus the agent group (ADK, FastAPI). No dev deps.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group agent --no-install-project --no-editable
COPY src ./src
RUN uv sync --frozen --no-dev --group agent --no-editable
COPY recall_demo ./recall_demo

RUN useradd --system --no-create-home app
USER app
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app

CMD ["python", "-m", "recall_demo.app"]
