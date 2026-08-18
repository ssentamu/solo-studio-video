#!/bin/bash
# Deploy Solo Studio Video on edgescout.tech/video
# Run on the VPS HOST: bash /docker/hermes-agent-r0tv/data/solo-studio-video/deploy-traefik.sh

set -euo pipefail

APP_DIR=/docker/hermes-agent-r0tv/data/solo-studio-video
: "${SOLO_STUDIO_API_TOKEN:?Set SOLO_STUDIO_API_TOKEN in the shell before deploying Solo Studio Video}"
SOLO_STUDIO_CORS_ORIGINS=${SOLO_STUDIO_CORS_ORIGINS:-https://edgescout.tech}
curl_config=""
cleanup() {
  if [ -n "$curl_config" ]; then
    rm -f "$curl_config"
  fi
}
trap cleanup EXIT

echo "=== Building Solo Studio Video ==="
cd "$APP_DIR"
docker build -t solo-studio-video:latest .

# Ensure jobs.json exists
touch "$APP_DIR/jobs.json"

echo ""
echo "=== Deploying on edgescout.tech/video ==="

# Remove old container
docker rm -f solo-studio-video 2>/dev/null || true

# Ensure jobs.json exists with correct ownership
touch "$APP_DIR/jobs.json"
chmod 666 "$APP_DIR/jobs.json"

# Launch with nginx on port 9091 (FastAPI on 8000 internally)
docker run -d \
  --name solo-studio-video \
  --network host \
  --restart unless-stopped \
  -e SOLO_STUDIO_API_TOKEN \
  -e SOLO_STUDIO_REQUIRE_API_TOKEN=1 \
  -e SOLO_STUDIO_CORS_ORIGINS="$SOLO_STUDIO_CORS_ORIGINS" \
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
curl -sSI https://edgescout.tech/video/ 2>&1 | head -5
curl -fsS https://edgescout.tech/video/api/health 2>&1
curl_config=$(mktemp)
chmod 600 "$curl_config"
printf 'header = "Authorization: Bearer %s"\n' "$SOLO_STUDIO_API_TOKEN" > "$curl_config"
curl -fsS --config "$curl_config" "https://edgescout.tech/video/api/jobs?limit=1" >/dev/null

echo ""
echo "=== URL ==="
echo "  https://edgescout.tech/video/"
echo ""
echo "Done."
