#!/bin/bash
# Deploy Solo Studio Video on edgescout.tech/video
# Run on the VPS HOST: bash /docker/hermes-agent-r0tv/data/solo-studio-video/deploy-traefik.sh

set -euo pipefail

APP_DIR=/docker/hermes-agent-r0tv/data/solo-studio-video

echo "=== Building Solo Studio Video ==="
cd "$APP_DIR"
docker build -t solo-studio-video:latest .

# Ensure jobs.json exists
touch "$APP_DIR/jobs.json"

echo ""
echo "=== Deploying on edgescout.tech/video ==="

# Remove old container
docker rm -f solo-studio-video 2>/dev/null || true

# Launch with nginx on port 9091 (FastAPI on 8000 internally)
docker run -d \
  --name solo-studio-video \
  --network host \
  --restart unless-stopped \
  -v "$APP_DIR/output:/app/output" \
  -v "$APP_DIR/jobs.json:/app/jobs.json" \
  -l "traefik.enable=true" \
  -l "traefik.http.routers.solo-video.rule=Host(\`edgescout.tech\`) && PathPrefix(\`/video\`)" \
  -l "traefik.http.routers.solo-video.priority=200" \
  -l "traefik.http.routers.solo-video.entrypoints=websecure" \
  -l "traefik.http.routers.solo-video.tls=true" \
  -l "traefik.http.routers.solo-video.tls.certresolver=letsencrypt" \
  -l "traefik.http.middlewares.solo-video-strip.stripprefix.prefixes=/video" \
  -l "traefik.http.routers.solo-video.middlewares=solo-video-strip" \
  -l "traefik.http.services.solo-video.loadbalancer.server.port=9091" \
  solo-studio-video:latest

echo ""
echo "Waiting for container to start..."
sleep 4

echo ""
echo "=== Container status ==="
docker ps --filter name=solo-studio-video --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"

echo ""
echo "=== Smoke test ==="
curl -sI https://edgescout.tech/video/ 2>&1 | head -5

echo ""
echo "=== URL ==="
echo "  https://edgescout.tech/video/"
echo ""
echo "Done."
