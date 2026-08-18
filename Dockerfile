FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV SOLO_STUDIO_REQUIRE_API_TOKEN=1

# Python dependencies
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    pyyaml

# Copy app code
COPY api.py /app/
COPY worker.py /app/
COPY package_utils.py /app/
COPY engines/ /app/engines/
COPY pipeline.py /app/
COPY frontend/ /app/frontend/
COPY templates.json /app/

# Create empty dirs for runtime
RUN mkdir -p /app/briefs /app/output

# Nginx: proxy /api/* and /* to FastAPI on :8000
RUN rm -f /etc/nginx/sites-enabled/default && \
    echo 'server { \
        listen 9091; \
        location / { \
            proxy_pass http://127.0.0.1:8000; \
            proxy_set_header Host \$host; \
            proxy_set_header X-Real-IP \$remote_addr; \
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for; \
            proxy_set_header X-Forwarded-Proto \$scheme; \
        } \
    }' > /etc/nginx/conf.d/default.conf

# Start script — fail the container if any critical process exits.
RUN printf '%s\n' \
    '#!/bin/bash' \
    'set -euo pipefail' \
    'nginx -g "daemon off;" &' \
    'nginx_pid=$!' \
    'cd /app' \
    'python api.py &' \
    'api_pid=$!' \
    'python worker.py &' \
    'worker_pid=$!' \
    'wait -n "$nginx_pid" "$api_pid" "$worker_pid"' \
    'status=$?' \
    'kill "$nginx_pid" "$api_pid" "$worker_pid" 2>/dev/null || true' \
    'wait "$nginx_pid" "$api_pid" "$worker_pid" 2>/dev/null || true' \
    'exit "$status"' \
    > /app/start.sh && chmod +x /app/start.sh

EXPOSE 9091
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:9091/api/health >/dev/null || exit 1
CMD ["/app/start.sh"]
