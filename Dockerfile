FROM ghcr.io/astral-sh/uv:0.11.17 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations migrations
COPY deploy/database-grants.sql deploy/database-grants.sql
COPY ledger ledger
USER 65532:65532
EXPOSE 8000
CMD ["uvicorn", "ledger.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
