#!/bin/bash
# Deploy Solo Studio Video on edgescout.tech/video
# Run on the VPS HOST: bash /docker/hermes-agent-r0tv/data/solo-studio-video/deploy-traefik.sh

set -euo pipefail

APP_DIR=/docker/hermes-agent-r0tv/data/solo-studio-video
APP_NAME=solo-studio-video
DOMAIN=https://edgescout.tech/video
PUBLIC_JOBS_URL=https://edgescout.tech/video/api/jobs?limit=1
: "${SOLO_STUDIO_API_TOKEN:?Set SOLO_STUDIO_API_TOKEN in the shell before deploying Solo Studio Video}"
SOLO_STUDIO_CORS_ORIGINS=${SOLO_STUDIO_CORS_ORIGINS:-https://edgescout.tech}
SOLO_STUDIO_ENABLE_HIGGSFIELD=${SOLO_STUDIO_ENABLE_HIGGSFIELD:-0}
SOLO_STUDIO_HIGGSFIELD_MODEL=${SOLO_STUDIO_HIGGSFIELD_MODEL:-seedance_2_0}
SOLO_STUDIO_HIGGSFIELD_RESOLUTION=${SOLO_STUDIO_HIGGSFIELD_RESOLUTION:-720p}
SOLO_STUDIO_HIGGSFIELD_TIMEOUT=${SOLO_STUDIO_HIGGSFIELD_TIMEOUT:-900}
SOLO_STUDIO_CURL_CONNECT_TIMEOUT=${SOLO_STUDIO_CURL_CONNECT_TIMEOUT:-5}
SOLO_STUDIO_CURL_MAX_TIME=${SOLO_STUDIO_CURL_MAX_TIME:-15}
HIGGSFIELD_CREDENTIALS_FILE=${HIGGSFIELD_CREDENTIALS_FILE:-$HOME/.config/higgsfield/credentials.json}
HIGGSFIELD_CREDENTIALS_MOUNT=()
CURL_BOUNDED=(--connect-timeout "$SOLO_STUDIO_CURL_CONNECT_TIMEOUT" --max-time "$SOLO_STUDIO_CURL_MAX_TIME")
release_tag=""
rollback_tag=""
curl_config=""
container_replaced=0

case "${SOLO_STUDIO_ENABLE_HIGGSFIELD,,}" in
  1|true|yes|on) SOLO_STUDIO_ENABLE_HIGGSFIELD=1 ;;
  0|false|no|off) SOLO_STUDIO_ENABLE_HIGGSFIELD=0 ;;
  *) echo "SOLO_STUDIO_ENABLE_HIGGSFIELD must be 0/1 or false/true." >&2; exit 1 ;;
esac

if [ "$SOLO_STUDIO_ENABLE_HIGGSFIELD" = "1" ]; then
  if [ ! -r "$HIGGSFIELD_CREDENTIALS_FILE" ]; then
    echo "SOLO_STUDIO_ENABLE_HIGGSFIELD=1 requires a readable Higgsfield credentials file; run 'higgsfield auth login' first." >&2
    exit 1
  fi
  HIGGSFIELD_CREDENTIALS_MOUNT=(
    -v "$HIGGSFIELD_CREDENTIALS_FILE:/root/.config/higgsfield/credentials.json:ro"
  )
fi

cleanup() {
  if [ -n "$curl_config" ]; then
    rm -f "$curl_config"
  fi
}

run_container() {
  local image_tag="$1"
  local container_name="${2:-$APP_NAME}"
  local network_mode="${3:-host}"
  docker run -d \
    --name "$container_name" \
    --network "$network_mode" \
    --restart unless-stopped \
    -e SOLO_STUDIO_API_TOKEN \
    -e SOLO_STUDIO_JOBS_FILE=/app/state/jobs.json \
    -e SOLO_STUDIO_REQUIRE_API_TOKEN=1 \
    -e SOLO_STUDIO_TRUST_PROXY_HEADERS=1 \
    -e SOLO_STUDIO_TRUSTED_PROXY_NETWORKS=127.0.0.1/32,::1/128 \
    -e SOLO_STUDIO_SESSION_COOKIE_PATH=/video \
    -e SOLO_STUDIO_CORS_ORIGINS="$SOLO_STUDIO_CORS_ORIGINS" \
    -e SOLO_STUDIO_ENABLE_HIGGSFIELD="$SOLO_STUDIO_ENABLE_HIGGSFIELD" \
    -e SOLO_STUDIO_HIGGSFIELD_MODEL="$SOLO_STUDIO_HIGGSFIELD_MODEL" \
    -e SOLO_STUDIO_HIGGSFIELD_RESOLUTION="$SOLO_STUDIO_HIGGSFIELD_RESOLUTION" \
    -e SOLO_STUDIO_HIGGSFIELD_TIMEOUT="$SOLO_STUDIO_HIGGSFIELD_TIMEOUT" \
    "${HIGGSFIELD_CREDENTIALS_MOUNT[@]}" \
    -v "$APP_DIR/output:/app/output" \
    -v "$APP_DIR/state:/app/state" \
    -l "traefik.enable=true" \
    -l "traefik.http.routers.solo-video.rule=Host(\`edgescout.tech\`) && PathPrefix(\`/video\`)" \
    -l "traefik.http.routers.solo-video.priority=200" \
    -l "traefik.http.routers.solo-video.entrypoints=websecure" \
    -l "traefik.http.routers.solo-video.tls=true" \
    -l "traefik.http.routers.solo-video.tls.certresolver=letsencrypt" \
    -l "traefik.http.middlewares.solo-video-strip.stripprefix.prefixes=/video" \
    -l "traefik.http.routers.solo-video.middlewares=solo-video-strip" \
    -l "traefik.http.services.solo-video.loadbalancer.server.port=9091" \
    "$image_tag"
}

run_container_preflight() {
  local image_tag="$1"
  local container_name="${2:-${APP_NAME}-rollback-preflight}"
  docker run -d \
    --name "$container_name" \
    --network none \
    -e SOLO_STUDIO_API_TOKEN \
    -e SOLO_STUDIO_JOBS_FILE=/app/state/jobs.json \
    -e SOLO_STUDIO_REQUIRE_API_TOKEN=1 \
    -e SOLO_STUDIO_DISABLE_WORKER=1 \
    -e SOLO_STUDIO_TRUST_PROXY_HEADERS=1 \
    -e SOLO_STUDIO_TRUSTED_PROXY_NETWORKS=127.0.0.1/32,::1/128 \
    -e SOLO_STUDIO_SESSION_COOKIE_PATH=/video \
    -e SOLO_STUDIO_CORS_ORIGINS="$SOLO_STUDIO_CORS_ORIGINS" \
    -e SOLO_STUDIO_ENABLE_HIGGSFIELD="$SOLO_STUDIO_ENABLE_HIGGSFIELD" \
    -e SOLO_STUDIO_HIGGSFIELD_MODEL="$SOLO_STUDIO_HIGGSFIELD_MODEL" \
    -e SOLO_STUDIO_HIGGSFIELD_RESOLUTION="$SOLO_STUDIO_HIGGSFIELD_RESOLUTION" \
    -e SOLO_STUDIO_HIGGSFIELD_TIMEOUT="$SOLO_STUDIO_HIGGSFIELD_TIMEOUT" \
    "$image_tag"
}

wait_for_local_health() {
  for attempt in $(seq 1 20); do
    if curl "${CURL_BOUNDED[@]}" -fsS http://127.0.0.1:9091/api/health >/dev/null \
      && curl "${CURL_BOUNDED[@]}" -fsS http://127.0.0.1:9091/video/api/health >/dev/null; then
      echo "LOCAL_HEALTH_OK attempt=$attempt"
      return 0
    fi
    if [ "$attempt" -lt 20 ]; then
      sleep 1
    fi
  done
  echo "Local container smoke failed after $attempt attempts" >&2
  return 1
}

wait_for_public_health() {
  for attempt in $(seq 1 20); do
    if curl "${CURL_BOUNDED[@]}" -sSIL "$DOMAIN/" >/dev/null \
      && curl "${CURL_BOUNDED[@]}" -fsS "$DOMAIN/api/health" >/dev/null \
      && [ "$(curl "${CURL_BOUNDED[@]}" -sS -o /dev/null -w '%{http_code}' "$PUBLIC_JOBS_URL")" = "401" ] \
      && curl "${CURL_BOUNDED[@]}" -fsS --config "$curl_config" "$PUBLIC_JOBS_URL" >/dev/null; then
      echo "PUBLIC_HEALTH_OK attempt=$attempt"
      return 0
    fi
    if [ "$attempt" -lt 20 ]; then
      sleep 2
    fi
  done
  echo "Public Traefik smoke failed after $attempt attempts" >&2
  return 1
}

preflight_rollback_image() {
  local preflight_name="${APP_NAME}-rollback-preflight-$$"
  docker rm -f "$preflight_name" >/dev/null 2>&1 || true
  if ! run_container_preflight "$rollback_tag" "$preflight_name" >/dev/null; then
    docker rm -f "$preflight_name" >/dev/null 2>&1 || true
    return 1
  fi
  local healthy=0
  for attempt in $(seq 1 20); do
    if docker exec "$preflight_name" curl -fsS --max-time 3 http://127.0.0.1:9091/api/health >/dev/null \
      && docker exec "$preflight_name" curl -fsS --max-time 3 http://127.0.0.1:9091/video/api/health >/dev/null; then
      healthy=1
      break
    fi
    sleep 1
  done
  docker rm -f "$preflight_name" >/dev/null 2>&1 || true
  if [ "$healthy" != "1" ]; then
    echo "Rollback image failed isolated local health preflight." >&2
    return 1
  fi
}

rollback_live() {
  local status=$?
  if [ "$status" -eq 0 ]; then
    return 0
  fi
  echo ""
  echo "=== Deploy failed; attempting rollback ===" >&2
  if [ "$container_replaced" != "1" ]; then
    echo "Container was not replaced yet; leaving existing service untouched." >&2
    exit "$status"
  fi
  if [ -z "$rollback_tag" ] || ! docker image inspect "$rollback_tag" >/dev/null 2>&1; then
    echo "No verified rollback image available; refusing to remove the current container." >&2
    exit "$status"
  fi
  if ! preflight_rollback_image; then
    echo "Rollback preflight failed; refusing to remove the current container." >&2
    exit "$status"
  fi
  docker rm -f "$APP_NAME" >/dev/null 2>&1 || true
  if ! run_container "$rollback_tag" >/dev/null; then
    echo "Rollback container could not be started from $rollback_tag." >&2
    exit "$status"
  fi
  for attempt in $(seq 1 20); do
    if curl "${CURL_BOUNDED[@]}" -fsS http://127.0.0.1:9091/api/health >/dev/null \
      && curl "${CURL_BOUNDED[@]}" -fsS http://127.0.0.1:9091/video/api/health >/dev/null \
      && curl "${CURL_BOUNDED[@]}" -fsS "$DOMAIN/api/health" >/dev/null \
      && [ "$(curl "${CURL_BOUNDED[@]}" -sS -o /dev/null -w '%{http_code}' "$PUBLIC_JOBS_URL")" = "401" ] \
      && curl "${CURL_BOUNDED[@]}" -fsS --config "$curl_config" "$PUBLIC_JOBS_URL" >/dev/null; then
      echo "Rolled back $APP_NAME to $rollback_tag; LOCAL_ROLLBACK_HEALTH_OK PUBLIC_ROLLBACK_HEALTH_OK attempt=$attempt" >&2
      exit "$status"
    fi
    sleep 1
  done
  echo "Rollback container failed local health checks: $rollback_tag" >&2
  exit "$status"
}

trap cleanup EXIT
trap rollback_live ERR

echo "=== Building Solo Studio Video ==="
cd "$APP_DIR"
timestamp=$(date -u +%Y%m%d%H%M%S%N)-$$
release_tag="solo-studio-video:release-$timestamp"
rollback_tag="solo-studio-video:rollback-$timestamp"

current_image=$(docker inspect -f '{{.Image}}' "$APP_NAME" 2>/dev/null || true)
if [ -z "$current_image" ] && docker image inspect "$APP_NAME:latest" >/dev/null 2>&1; then
  current_image="$APP_NAME:latest"
fi
if [ -n "$current_image" ]; then
  docker tag "$current_image" "$rollback_tag"
  docker image inspect "$rollback_tag" >/dev/null
  echo "Tagged rollback image: $rollback_tag"
else
  echo "No existing $APP_NAME container found; rollback image not created."
fi

docker build -t "$release_tag" .
docker tag "$release_tag" "$APP_NAME:latest"

# Ensure shared state file exists with correct host/container permissions.
mkdir -p "$APP_DIR/state"
if [ ! -e "$APP_DIR/state/jobs.json" ]; then
  if [ -f "$APP_DIR/jobs.json" ]; then
    cp -p "$APP_DIR/jobs.json" "$APP_DIR/state/jobs.json"
  else
    touch "$APP_DIR/state/jobs.json"
  fi
fi
chmod 660 "$APP_DIR/state/jobs.json"

curl_config=$(mktemp)
chmod 600 "$curl_config"
printf '%s\n' "header = \"Authorization: Bearer $SOLO_STUDIO_API_TOKEN\"" > "$curl_config"

echo ""
echo "=== Deploying on edgescout.tech/video ==="
container_replaced=0
if docker inspect "$APP_NAME" >/dev/null 2>&1; then
  docker rm -f "$APP_NAME"
  container_replaced=1
fi
if [ "$container_replaced" != "1" ] && [ -n "$current_image" ]; then
  echo "Existing container could not be removed; refusing replacement." >&2
  exit 1
fi
container_replaced=1
run_container "$release_tag" >/dev/null

echo ""
echo "Waiting for container to start..."
sleep 4

echo ""
echo "=== Container status ==="
docker ps --filter name="$APP_NAME" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"

echo ""
echo "=== Local smoke test ==="
wait_for_local_health
curl "${CURL_BOUNDED[@]}" -fsS http://127.0.0.1:9091/api/health
curl "${CURL_BOUNDED[@]}" -fsS http://127.0.0.1:9091/video/api/health

echo ""
echo "=== Public Traefik smoke test ==="
wait_for_public_health

curl "${CURL_BOUNDED[@]}" -sSI "$DOMAIN/" 2>&1 | head -5
curl "${CURL_BOUNDED[@]}" -fsS "$DOMAIN/api/health" 2>&1
curl "${CURL_BOUNDED[@]}" -fsS --config "$curl_config" "$PUBLIC_JOBS_URL" >/dev/null

echo ""
echo "=== URL ==="
echo "  $DOMAIN/"
echo ""
echo "Done."
