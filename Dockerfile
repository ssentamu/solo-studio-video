FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    ffmpeg \
    espeak-ng \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY api.py /app/
COPY worker.py /app/
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

# Start script
RUN printf '#!/bin/bash\nset -e\nnginx\ncd /app\npython api.py &\nsleep 2\npython worker.py &\nwait\n' > /app/start.sh && chmod +x /app/start.sh

EXPOSE 9091
CMD ["/app/start.sh"]
