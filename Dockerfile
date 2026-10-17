FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip sqlite3 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN python3 -m pip install --no-cache-dir . pyyaml && python3 scripts/seed_db.py --if-missing
EXPOSE 8000
CMD ["python3", "scripts/run_backend.py"]
