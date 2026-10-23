FROM ghcr.io/astral-sh/uv:0.12.21 AS uv
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONPATH=/app/src PATH=/app/.venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --python /usr/bin/python3 && .venv/bin/python scripts/seed_db.py --if-missing
EXPOSE 8000
CMD [".venv/bin/python", "scripts/run_backend.py"]
