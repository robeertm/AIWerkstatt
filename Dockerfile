# --- Stage 1: build the React frontend ---
FROM node:20-alpine AS build
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# --- Stage 2: serve backend + built frontend (the web control plane) ---
FROM python:3.12-slim
WORKDIR /app
# gosu: drop from root (volume setup) to the app user at startup.
# gosu: drop root→app at startup. git: push a project to GitHub on "Publish".
RUN apt-get update && apt-get install -y --no-install-recommends gosu git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
# The version single-source: config.py reads it at runtime so the self-update
# check always reports the version the build actually shipped.
COPY pyproject.toml ./
COPY --from=build /fe/dist ./dist
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENV AIWERKSTATT_DIST=/app/dist
EXPOSE 8095
# The orchestrator runs as a background thread in this single worker; long agent
# runs happen in separate containers and never block the HTTP worker.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8095", "app:app", "--timeout", "120"]
