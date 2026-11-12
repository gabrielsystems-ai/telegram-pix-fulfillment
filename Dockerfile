FROM ghcr.io/astral-sh/uv:0.11.19 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_NO_CACHE=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-install-project && uv pip install --no-deps -e .
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY examples/ ./examples/
RUN useradd --create-home --uid 10001 app
USER app
CMD ["python", "-m", "vip.deployment", "check"]
