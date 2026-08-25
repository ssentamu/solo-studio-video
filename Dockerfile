FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    ffmpeg \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV SOLO_STUDIO_REQUIRE_API_TOKEN=1
ENV SOLO_STUDIO_DATABASE_FILE=/app/state/solo_studio.sqlite3
ENV HOME=/home/solo

# Pin the provider CLI in the image. Real generation remains opt-in and still
# requires an authenticated credentials file at deployment time.
ARG HIGGSFIELD_CLI_VERSION=1.1.23
RUN npm install --global "@higgsfield/cli@${HIGGSFIELD_CLI_VERSION}" \
    && higgsfield --version

# Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy app code
COPY api.py /app/
COPY worker.py /app/
COPY runtime_init.py /app/
COPY job_store.py /app/
COPY auth_store.py /app/
COPY media_assembly.py /app/
COPY audio_generation.py /app/
COPY package_utils.py /app/
COPY engines/ /app/engines/
COPY pipeline.py /app/
COPY frontend/ /app/frontend/
COPY templates.json /app/

# Create empty dirs for runtime
RUN mkdir -p /app/briefs /app/output /app/state /home/solo/.config/higgsfield && \
    useradd --system --uid 10001 --create-home --home-dir /home/solo solo && \
    chown -R solo:solo /app /home/solo

# Nginx: proxy /api/* and /* to FastAPI on :8000. Keep the complete config
# explicit so base-image log, pid, and temp paths remain non-root safe.
RUN mkdir -p /tmp/nginx-client /tmp/nginx-proxy /tmp/nginx-fastcgi /tmp/nginx-uwsgi /tmp/nginx-scgi && \
    chown -R solo:solo /tmp/nginx-client /tmp/nginx-proxy /tmp/nginx-fastcgi /tmp/nginx-uwsgi /tmp/nginx-scgi && \
    chmod 1777 /tmp/nginx-client /tmp/nginx-proxy /tmp/nginx-fastcgi /tmp/nginx-uwsgi /tmp/nginx-scgi && \
    rm -f /etc/nginx/sites-enabled/default && \
    printf '%s\n' \
    'worker_processes 1;' \
    'pid /tmp/nginx.pid;' \
    'error_log /dev/stderr warn;' \
    'events { worker_connections 1024; }' \
    'http {' \
    '    include /etc/nginx/mime.types;' \
    '    default_type application/octet-stream;' \
    '    access_log /dev/stdout;' \
    '    sendfile on;' \
    '    client_body_temp_path /tmp/nginx-client;' \
    '    proxy_temp_path /tmp/nginx-proxy;' \
    '    fastcgi_temp_path /tmp/nginx-fastcgi;' \
    '    uwsgi_temp_path /tmp/nginx-uwsgi;' \
    '    scgi_temp_path /tmp/nginx-scgi;' \
    '    include /etc/nginx/conf.d/*.conf;' \
    '}' > /etc/nginx/nginx.conf && \
    echo 'server { \
        listen 127.0.0.1:9091; \
        location / { \
            proxy_pass http://127.0.0.1:8000; \
            proxy_set_header Host $host; \
            proxy_set_header X-Real-IP $remote_addr; \
            proxy_set_header X-Forwarded-For $remote_addr; \
            proxy_set_header X-Forwarded-Proto $scheme; \
        } \
    }' > /etc/nginx/conf.d/default.conf && \
    nginx -t -c /etc/nginx/nginx.conf && \
    rm -f /tmp/nginx.pid

# Start script — fail the container if any critical process exits.
RUN printf '%s\n' \
    '#!/bin/bash' \
    'set -euo pipefail' \
    'nginx -g "error_log /dev/stderr warn; daemon off;" &' \
    'nginx_pid=$!' \
    'cd /app' \
    'python runtime_init.py' \
    'python api.py &' \
    'api_pid=$!' \
    'worker_pid=""' \
    'if [ "${SOLO_STUDIO_DISABLE_WORKER:-0}" != "1" ]; then' \
    '  python worker.py &' \
    '  worker_pid=$!' \
    'fi' \
    'term_handler() {' \
    '  kill "$nginx_pid" "$api_pid" ${worker_pid:-} 2>/dev/null || true' \
    '  wait "$nginx_pid" "$api_pid" ${worker_pid:-} 2>/dev/null || true' \
    '  exit 143' \
    '}' \
    'trap term_handler TERM INT HUP' \
    'set +e' \
    'if [ -n "$worker_pid" ]; then' \
    '  wait -n "$nginx_pid" "$api_pid" "$worker_pid"' \
    'else' \
    '  wait -n "$nginx_pid" "$api_pid"' \
    'fi' \
    'status=$?' \
    'set -e' \
    'if [ -n "$worker_pid" ]; then' \
    '  kill "$nginx_pid" "$api_pid" "$worker_pid" 2>/dev/null || true' \
    '  wait "$nginx_pid" "$api_pid" "$worker_pid" 2>/dev/null || true' \
    'else' \
    '  kill "$nginx_pid" "$api_pid" 2>/dev/null || true' \
    '  wait "$nginx_pid" "$api_pid" 2>/dev/null || true' \
    'fi' \
    'exit "$status"' \
    > /app/start.sh && chmod +x /app/start.sh

EXPOSE 9091
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:9091/api/health >/dev/null || exit 1
USER solo
CMD ["/app/start.sh"]
