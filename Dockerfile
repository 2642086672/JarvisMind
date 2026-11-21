# syntax=docker/dockerfile:1

# Stage 1: build the browser console.
FROM node:20-bookworm-slim AS console
WORKDIR /src/console
COPY console/package.json console/package-lock.json ./
RUN npm ci
COPY console/ ./
RUN npm run build

# Stage 2: the API server with the built console.
FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JARVISMIND_DATA_DIR=/data \
    JARVISMIND_HOST=0.0.0.0 \
    JARVISMIND_ALLOW_REMOTE=1 \
    JARVISMIND_PORT=8777 \
    JARVISMIND_CONSOLE_DIR=/app/console/dist
RUN useradd --uid 10001 --create-home jarvis \
 && mkdir -p /data /app/backend /app/docker /app/console \
 && chown -R jarvis:jarvis /data
WORKDIR /app/backend
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
COPY backend/ ./
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
COPY --from=console /src/console/dist /app/console/dist
USER jarvis
VOLUME ["/data"]
EXPOSE 8777
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8777/health', timeout=5)" || exit 1
ENTRYPOINT ["sh", "/app/docker/entrypoint.sh"]
