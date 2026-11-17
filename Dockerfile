FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHON_EXECUTABLE="/app/.venv/bin/python"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY configs ./configs
COPY contracts ./contracts
COPY dataset ./dataset
COPY services ./services
COPY evals ./evals
COPY scripts ./scripts

RUN mkdir -p /app/data /app/data/traces /app/evals/reports

EXPOSE 8000 8101 8102

CMD ["uvicorn", "services.gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
