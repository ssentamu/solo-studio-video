#!/bin/bash
# Deploy Solo Studio Video on edgescout.tech/video
# Run on the VPS HOST: bash /docker/hermes-agent-r0tv/data/solo-studio-video/deploy-traefik.sh

set -euo pipefail

APP_DIR=/docker/hermes-agent-r0tv/data/solo-studio-video
APP_NAME=solo-studio-video
DOMAIN=https://edgescout.tech/video
PUBLIC_JOBS_URL=https://edgescout.tech/video/api/jobs?limit=1
SOLO_STUDIO_API_TOKEN_FILE=${SOLO_STUDIO_API_TOKEN_FILE:-$HOME/.config/solo-studio-video/api_token}
SOLO_STUDIO_API_TOKEN_EPHEMERAL=0
SOLO_STUDIO_API_TOKEN_TEMP_DIR=""

cleanup_ephemeral_api_token() {
  if [ "$SOLO_STUDIO_API_TOKEN_EPHEMERAL" = "1" ] && [ -n "$SOLO_STUDIO_API_TOKEN_FILE" ]; then
    rm -f -- "$SOLO_STUDIO_API_TOKEN_FILE" 2>/dev/null || true
  fi
  if [ "$SOLO_STUDIO_API_TOKEN_EPHEMERAL" = "1" ] && [ -n "$SOLO_STUDIO_API_TOKEN_TEMP_DIR" ]; then
    rm -rf -- "$SOLO_STUDIO_API_TOKEN_TEMP_DIR" 2>/dev/null || true
  fi
}
trap cleanup_ephemeral_api_token EXIT

if [ ! -e "$SOLO_STUDIO_API_TOKEN_FILE" ] && [ -n "${SOLO_STUDIO_API_TOKEN:-}" ]; then
  fallback_token_dir="${TMPDIR:-/tmp}/solo-studio-video-token.${BASHPID}.${RANDOM}"
  if ! (umask 077 && mkdir -- "$fallback_token_dir"); then
    echo "Could not create a private temporary directory for the API token." >&2
    exit 1
  fi
  SOLO_STUDIO_API_TOKEN_TEMP_DIR="$fallback_token_dir"
  SOLO_STUDIO_API_TOKEN_EPHEMERAL=1
  fallback_token_template="$fallback_token_dir/token.XXXXXX"
  if SOLO_STUDIO_API_TOKEN_FILE=$(mktemp "$fallback_token_template"); then
    :
  else
    token_create_status=$?
    echo "Could not create the temporary API token file." >&2
    exit "$token_create_status"
  fi
  chmod 600 "$SOLO_STUDIO_API_TOKEN_FILE"
  printf '%s\n' "$SOLO_STUDIO_API_TOKEN" > "$SOLO_STUDIO_API_TOKEN_FILE"
fi
if [ -L "$SOLO_STUDIO_API_TOKEN_FILE" ] || [ ! -f "$SOLO_STUDIO_API_TOKEN_FILE" ] || [ ! -r "$SOLO_STUDIO_API_TOKEN_FILE" ]; then
  echo "SOLO_STUDIO_API_TOKEN_FILE must be an existing readable regular file (not a symlink)." >&2
  exit 1
fi
if [ "$SOLO_STUDIO_API_TOKEN_EPHEMERAL" = "0" ]; then
  secret_parent=$(dirname -- "$SOLO_STUDIO_API_TOKEN_FILE")
  if [ ! -d "$secret_parent" ]; then
    echo "SOLO_STUDIO_API_TOKEN_FILE parent directory must exist." >&2
    exit 1
  fi
  secret_parent_mode=$(stat -c '%a' "$secret_parent")
  secret_parent_owner=$(stat -c '%u' "$secret_parent")
  if (( (8#$secret_parent_mode & 077) != 0 )) || [ "$secret_parent_owner" != "$(id -u)" ]; then
    echo "SOLO_STUDIO_API_TOKEN_FILE parent directory must be owned by the deploying user and private." >&2
    exit 1
  fi
fi
secret_mode=$(stat -c '%a' "$SOLO_STUDIO_API_TOKEN_FILE")
if (( (8#$secret_mode & 077) != 0 )); then
  echo "SOLO_STUDIO_API_TOKEN_FILE must not be readable by group or other users." >&2
  exit 1
fi
SOLO_STUDIO_API_TOKEN_MOUNT=(
  --mount "type=bind,src=$SOLO_STUDIO_API_TOKEN_FILE,dst=/run/secrets/solo_studio_api_token,ro"
)
SOLO_STUDIO_CORS_ORIGINS=${SOLO_STUDIO_CORS_ORIGINS:-https://edgescout.tech}
SOLO_STUDIO_ENABLE_HIGGSFIELD=${SOLO_STUDIO_ENABLE_HIGGSFIELD:-0}
SOLO_STUDIO_HIGGSFIELD_MODEL=${SOLO_STUDIO_HIGGSFIELD_MODEL:-seedance_2_0}
SOLO_STUDIO_HIGGSFIELD_RESOLUTION=${SOLO_STUDIO_HIGGSFIELD_RESOLUTION:-1080p}
SOLO_STUDIO_HIGGSFIELD_TIMEOUT=${SOLO_STUDIO_HIGGSFIELD_TIMEOUT:-900}
SOLO_STUDIO_CURL_CONNECT_TIMEOUT=${SOLO_STUDIO_CURL_CONNECT_TIMEOUT:-5}
SOLO_STUDIO_CURL_MAX_TIME=${SOLO_STUDIO_CURL_MAX_TIME:-15}
SOLO_STUDIO_CONTAINER_MEMORY=${SOLO_STUDIO_CONTAINER_MEMORY:-2g}
SOLO_STUDIO_CONTAINER_CPUS=${SOLO_STUDIO_CONTAINER_CPUS:-2}
SOLO_STUDIO_CONTAINER_PIDS_LIMIT=${SOLO_STUDIO_CONTAINER_PIDS_LIMIT:-512}
HIGGSFIELD_CREDENTIALS_FILE=${HIGGSFIELD_CREDENTIALS_FILE:-$HOME/.config/higgsfield/credentials.json}
HIGGSFIELD_CREDENTIALS_MOUNT=()
CURL_BOUNDED=(--connect-timeout "$SOLO_STUDIO_CURL_CONNECT_TIMEOUT" --max-time "$SOLO_STUDIO_CURL_MAX_TIME")
DOCKER_RESOURCE_ARGS=(--memory "$SOLO_STUDIO_CONTAINER_MEMORY" --cpus "$SOLO_STUDIO_CONTAINER_CPUS" --pids-limit "$SOLO_STUDIO_CONTAINER_PIDS_LIMIT")
CONTAINER_USER_ARGS=()
release_tag=""
rollback_tag=""
curl_config=""
curl_config_dir=""
container_replaced=0
removal_started=0
removal_confirmed=0
replacement_started=0
active_child_pid=""
PREFLIGHT_STATE_DIR=""
PREFLIGHT_OUTPUT_DIR=""
PREFLIGHT_DIRS_SAFE_TO_DELETE=0
existing_container_found=0

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
    -v "$HIGGSFIELD_CREDENTIALS_FILE:/home/solo/.config/higgsfield/credentials.json:ro"
  )
fi

cleanup() {
  if [ "$SOLO_STUDIO_API_TOKEN_EPHEMERAL" = "1" ] && [ -n "$SOLO_STUDIO_API_TOKEN_FILE" ]; then
    rm -f -- "$SOLO_STUDIO_API_TOKEN_FILE" 2>/dev/null || true
  fi
  if [ "$SOLO_STUDIO_API_TOKEN_EPHEMERAL" = "1" ] && [ -n "$SOLO_STUDIO_API_TOKEN_TEMP_DIR" ]; then
    rm -rf -- "$SOLO_STUDIO_API_TOKEN_TEMP_DIR" 2>/dev/null || true
  fi
  if [ -n "$curl_config" ]; then
    rm -f "$curl_config"
  fi
  if [ -n "$curl_config_dir" ]; then
    rmdir "$curl_config_dir" 2>/dev/null || true
  fi
  if [ "$PREFLIGHT_DIRS_SAFE_TO_DELETE" = "1" ]; then
    if [ -n "$PREFLIGHT_STATE_DIR" ]; then
      rm -rf "$PREFLIGHT_STATE_DIR"
    fi
    if [ -n "$PREFLIGHT_OUTPUT_DIR" ]; then
      rm -rf "$PREFLIGHT_OUTPUT_DIR"
    fi
  elif [ -n "$PREFLIGHT_STATE_DIR" ] || [ -n "$PREFLIGHT_OUTPUT_DIR" ]; then
    echo "Preserving preflight directories because Docker container absence was not verified." >&2
  fi
  PREFLIGHT_STATE_DIR=""
  PREFLIGHT_OUTPUT_DIR=""
}

clear_preflight_dirs() {
  if [ "$PREFLIGHT_DIRS_SAFE_TO_DELETE" != "1" ]; then
    echo "Refusing to delete preflight directories before Docker container absence is verified." >&2
    return 1
  fi
  if [ -n "$PREFLIGHT_STATE_DIR" ]; then
    rm -rf "$PREFLIGHT_STATE_DIR"
  fi
  if [ -n "$PREFLIGHT_OUTPUT_DIR" ]; then
    rm -rf "$PREFLIGHT_OUTPUT_DIR"
  fi
  PREFLIGHT_STATE_DIR=""
  PREFLIGHT_OUTPUT_DIR=""
  PREFLIGHT_DIRS_SAFE_TO_DELETE=0
}

run_tracked() {
  "$@" &
  active_child_pid=$!
  local status=0
  wait "$active_child_pid" || status=$?
  active_child_pid=""
  return "$status"
}

copy_regular_file_excl() {
  # Copy source to a temporary file and atomically publish a new target without
  # following symlinks on either side.  A failed copy never leaves a partial
  # state file at the destination.
  python3 -c '
import os, shutil, stat, sys, tempfile
mode = int(sys.argv[3], 8)
source_fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
target_dir = os.path.dirname(sys.argv[2]) or "."
temp_fd, temp_path = tempfile.mkstemp(prefix="." + os.path.basename(sys.argv[2]) + ".tmp-", dir=target_dir)
os.close(temp_fd)
try:
    if not stat.S_ISREG(os.fstat(source_fd).st_mode):
        raise ValueError("source must be a regular file")
    target_fd = os.open(temp_path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        if not stat.S_ISREG(os.fstat(target_fd).st_mode):
            raise ValueError("target must be a regular file")
        with os.fdopen(source_fd, "rb", closefd=False) as source, os.fdopen(target_fd, "wb", closefd=False) as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
    finally:
        os.close(target_fd)
    os.link(temp_path, sys.argv[2], follow_symlinks=False)
    directory_fd = os.open(target_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass
    os.close(source_fd)
' "$1" "$2" "$3"
}

copy_sqlite_database_excl() {
  # Take a consistent SQLite backup, then atomically publish it.  This keeps
  # preflight aligned with a populated live database without copying a file
  # mid-transaction.
  python3 -c '
import os, sqlite3, stat, sys, tempfile
source_fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
target_dir = os.path.dirname(sys.argv[2]) or "."
temp_fd, temp_path = tempfile.mkstemp(prefix="." + os.path.basename(sys.argv[2]) + ".tmp-", dir=target_dir)
os.close(temp_fd)
source = target = None
try:
    if not stat.S_ISREG(os.fstat(source_fd).st_mode):
        raise ValueError("SQLite source must be a regular file")
    source = sqlite3.connect(f"file:/proc/self/fd/{source_fd}?mode=ro", uri=True)
    target = sqlite3.connect(temp_path)
    source.backup(target)
    target.commit()
    target.close()
    target = None
    file_fd = os.open(temp_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ValueError("SQLite target must be a regular file")
        os.fsync(file_fd)
    finally:
        os.close(file_fd)
    os.link(temp_path, sys.argv[2], follow_symlinks=False)
    directory_fd = os.open(target_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if target is not None:
        target.close()
    if source is not None:
        source.close()
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass
    os.close(source_fd)
' "$1" "$2"
}

container_inspect_state() {
  local container_name="$1"
  local inspect_error=""
  local inspect_status=0
  if inspect_error=$(docker container inspect "$container_name" 2>&1 >/dev/null); then
    return 0
  else
    inspect_status=$?
  fi
  if grep -Eiq "No such (object|container)" <<<"$inspect_error"; then
    return 1
  fi
  echo "Docker inspect for $container_name failed (status=$inspect_status); container state is unknown." >&2
  return 2
}

remove_container_and_verify() {
  local container_name="$1"
  local remove_status=0
  local inspect_status=0
  if run_tracked docker rm -f "$container_name" >/dev/null 2>&1; then
    :
  else
    remove_status=$?
  fi
  if container_inspect_state "$container_name"; then
    echo "Docker still reports container $container_name after removal." >&2
    return 1
  else
    inspect_status=$?
  fi
  case "$inspect_status" in
    1)
      if [ "$remove_status" -ne 0 ]; then
        echo "docker rm for $container_name returned status=$remove_status, but Docker confirmed the container is absent." >&2
      fi
      return 0
      ;;
    *)
      echo "Refusing to continue after removing $container_name because Docker did not confirm its absence." >&2
      return 1
      ;;
  esac
}

reconcile_replacement() {
  local inspect_status=0
  if container_inspect_state "$APP_NAME"; then
    container_replaced=1
    return 0
  else
    inspect_status=$?
  fi
  case "$inspect_status" in
    1)
      container_replaced=0
      return 1
      ;;
    *)
      echo "Replacement container state is unknown; refusing destructive rollback actions." >&2
      return 2
      ;;
  esac
}

start_container_and_reconcile() {
  local image_tag="$1"
  local run_status=0
  local reconcile_status=0
  replacement_started=1
  container_replaced=0
  if run_container "$image_tag" >/dev/null; then
    :
  else
    run_status=$?
  fi
  if reconcile_replacement; then
    if [ "$run_status" -ne 0 ]; then
      echo "docker run returned status=$run_status, but Docker confirmed the replacement container exists; continuing with health checks." >&2
    fi
    return 0
  else
    reconcile_status=$?
  fi
  case "$reconcile_status" in
    1)
      echo "Replacement container is absent after docker run (status=$run_status)." >&2
      return 1
      ;;
    *)
      echo "Replacement container existence could not be reconciled after docker run; refusing rollback mutation." >&2
      return 2
      ;;
  esac
}

run_container() {
  local image_tag="$1"
  local container_name="${2:-$APP_NAME}"
  local network_mode="${3:-host}"
  run_tracked docker run -d \
    --name "$container_name" \
    --network "$network_mode" \
    --restart unless-stopped \
    "${DOCKER_RESOURCE_ARGS[@]}" \
    "${CONTAINER_USER_ARGS[@]}" \
    -e SOLO_STUDIO_API_TOKEN_FILE=/run/secrets/solo_studio_api_token \
    -e SOLO_STUDIO_JOBS_FILE=/app/state/jobs.json \
    -e SOLO_STUDIO_DATABASE_FILE=/app/state/solo_studio.sqlite3 \
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
    "${SOLO_STUDIO_API_TOKEN_MOUNT[@]}" \
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
  run_tracked docker run -d \
    --name "$container_name" \
    --network none \
    "${DOCKER_RESOURCE_ARGS[@]}" \
    "${CONTAINER_USER_ARGS[@]}" \
    -e SOLO_STUDIO_API_TOKEN_FILE=/run/secrets/solo_studio_api_token \
    -e SOLO_STUDIO_JOBS_FILE=/app/state/jobs.json \
    -e SOLO_STUDIO_DATABASE_FILE=/app/state/solo_studio.sqlite3 \
    -e SOLO_STUDIO_REQUIRE_API_TOKEN=1 \
    -e SOLO_STUDIO_EXPECTED_RUNTIME_UID="$runtime_uid" \
    -e SOLO_STUDIO_EXPECTED_RUNTIME_GID="$runtime_gid" \
    -e SOLO_STUDIO_TRUST_PROXY_HEADERS=1 \
    -e SOLO_STUDIO_TRUSTED_PROXY_NETWORKS=127.0.0.1/32,::1/128 \
    -e SOLO_STUDIO_SESSION_COOKIE_PATH=/video \
    -e SOLO_STUDIO_CORS_ORIGINS="$SOLO_STUDIO_CORS_ORIGINS" \
    -e SOLO_STUDIO_ENABLE_HIGGSFIELD="$SOLO_STUDIO_ENABLE_HIGGSFIELD" \
    -e SOLO_STUDIO_HIGGSFIELD_MODEL="$SOLO_STUDIO_HIGGSFIELD_MODEL" \
    -e SOLO_STUDIO_HIGGSFIELD_RESOLUTION="$SOLO_STUDIO_HIGGSFIELD_RESOLUTION" \
    -e SOLO_STUDIO_HIGGSFIELD_TIMEOUT="$SOLO_STUDIO_HIGGSFIELD_TIMEOUT" \
    -v "${PREFLIGHT_OUTPUT_DIR:-$APP_DIR/output}:/app/output" \
    -v "${PREFLIGHT_STATE_DIR:-$APP_DIR/state}:/app/state" \
    "${HIGGSFIELD_CREDENTIALS_MOUNT[@]}" \
    "${SOLO_STUDIO_API_TOKEN_MOUNT[@]}" \
    "$image_tag"
}

preflight_image() {
  local image_tag="$1"
  local preflight_label="$2"
  local preflight_name="${APP_NAME}-${preflight_label}-preflight-$$"
  local start_status=0
  local healthy=0

  # Reconcile a stale preflight before creating new bind-mount directories.  An
  # unknown Docker state is fail-closed and leaves any existing state untouched.
  PREFLIGHT_DIRS_SAFE_TO_DELETE=0
  if ! remove_container_and_verify "$preflight_name"; then
    echo "Refusing to start isolated preflight while the prior preflight container state is unknown." >&2
    return 1
  fi

  PREFLIGHT_STATE_DIR=$(mktemp -d)
  PREFLIGHT_OUTPUT_DIR=$(mktemp -d)
  chmod 0700 "$PREFLIGHT_STATE_DIR" "$PREFLIGHT_OUTPUT_DIR"
  chown "$runtime_uid:$runtime_gid" "$PREFLIGHT_STATE_DIR" "$PREFLIGHT_OUTPUT_DIR"
  # Seed the isolated state with a copy of the real legacy jobs file so the
  # preflight exercises the same startup import the live container will run.
  # An image that cannot ingest current production state must fail here,
  # before the live container is replaced.
  if [ -L "$APP_DIR/state/jobs.json" ] || { [ -e "$APP_DIR/state/jobs.json" ] && [ ! -f "$APP_DIR/state/jobs.json" ]; }; then
    echo "Refusing symlinked or non-regular state/jobs.json as the preflight import source." >&2
    PREFLIGHT_DIRS_SAFE_TO_DELETE=1
    clear_preflight_dirs
    return 1
  elif [ -f "$APP_DIR/state/jobs.json" ]; then
    if ! copy_regular_file_excl "$APP_DIR/state/jobs.json" "$PREFLIGHT_STATE_DIR/jobs.json" 600; then
      echo "Could not copy state/jobs.json into the isolated preflight state directory." >&2
      PREFLIGHT_DIRS_SAFE_TO_DELETE=1
      clear_preflight_dirs
      return 1
    fi
  else
    printf '{}\n' > "$PREFLIGHT_STATE_DIR/jobs.json"
  fi
  chmod 0600 "$PREFLIGHT_STATE_DIR/jobs.json"
  chown "$runtime_uid:$runtime_gid" "$PREFLIGHT_STATE_DIR/jobs.json"
  if [ -L "$APP_DIR/state/solo_studio.sqlite3" ] || { [ -e "$APP_DIR/state/solo_studio.sqlite3" ] && [ ! -f "$APP_DIR/state/solo_studio.sqlite3" ]; }; then
    echo "Refusing symlinked or non-regular state/solo_studio.sqlite3 as the preflight database source." >&2
    PREFLIGHT_DIRS_SAFE_TO_DELETE=1
    clear_preflight_dirs
    return 1
  elif [ -f "$APP_DIR/state/solo_studio.sqlite3" ]; then
    if ! copy_sqlite_database_excl "$APP_DIR/state/solo_studio.sqlite3" "$PREFLIGHT_STATE_DIR/solo_studio.sqlite3"; then
      echo "Could not snapshot state/solo_studio.sqlite3 into the isolated preflight state directory." >&2
      PREFLIGHT_DIRS_SAFE_TO_DELETE=1
      clear_preflight_dirs
      return 1
    fi
    chmod 0600 "$PREFLIGHT_STATE_DIR/solo_studio.sqlite3"
    chown "$runtime_uid:$runtime_gid" "$PREFLIGHT_STATE_DIR/solo_studio.sqlite3"
  fi

  if run_container_preflight "$image_tag" "$preflight_name" >/dev/null; then
    :
  else
    start_status=$?
  fi
  if [ "$start_status" -ne 0 ]; then
    # A failed docker run may still have created a container.  Reconcile it
    # before cleanup; never exec against it and never remove its mounts while
    # Docker's absence check is ambiguous.
    PREFLIGHT_DIRS_SAFE_TO_DELETE=0
    if ! remove_container_and_verify "$preflight_name"; then
      echo "Preflight startup failed and container removal was not verified; preserving temporary bind-mount directories." >&2
      return 1
    fi
    PREFLIGHT_DIRS_SAFE_TO_DELETE=1
    clear_preflight_dirs
    echo "Release image could not start in isolated preflight." >&2
    return 1
  fi

  # Keep every runtime check in one docker exec while the preflight container
  # is alive.  Removal and mount cleanup happen only after this loop finishes.
  for attempt in $(seq 1 20); do
    if run_tracked docker exec "$preflight_name" sh -ceu '
      test "$(id -u)" = "$SOLO_STUDIO_EXPECTED_RUNTIME_UID"
      test "$(id -g)" = "$SOLO_STUDIO_EXPECTED_RUNTIME_GID"
      test -w /app/state && test -w /app/output
      python -c "import api, job_store, worker"
      test "$(curl -sS --max-time 3 -o /dev/null -w "%{http_code}" http://127.0.0.1:9091/api/health)" = "200"
      test "$(curl -sS --max-time 3 -o /dev/null -w "%{http_code}" http://127.0.0.1:9091/api/templates)" = "200"
      test "$(curl -sS --max-time 3 -o /dev/null -w "%{http_code}" http://127.0.0.1:9091/api/jobs?limit=1)" = "401"
      token_config_dir=$(mktemp -d); chmod 700 "$token_config_dir"; token_config="$token_config_dir/config"
      TOKEN_CONFIG="$token_config" python -c "import api, os, pathlib, sys; token = api._read_secure_text_file(pathlib.Path(\"/run/secrets/solo_studio_api_token\"), 4096); token = token[:-1] if token.endswith(\"\\n\") else token; invalid = (not token) or any(ord(character) < 0x20 or ord(character) == 0x7F or ord(character) in (34, 92) for character in token); sys.exit(\"invalid token\") if invalid else None; config = (\"header = \" + chr(34) + \"Authorization: Bearer \" + token + chr(34) + chr(10)).encode(\"utf-8\"); config_fd = os.open(os.environ[\"TOKEN_CONFIG\"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600); written = os.write(config_fd, config); os.fsync(config_fd); os.close(config_fd); sys.exit(\"short curl config write\") if written != len(config) else None"
      test "$(curl -sS --max-time 3 -o /dev/null -w "%{http_code}" --config "$token_config" http://127.0.0.1:9091/api/jobs?limit=1)" = "200"
      rm -rf "$token_config_dir"
    '; then
      healthy=1
      break
    fi
    sleep 1
  done

  # The container is still alive through the final exec above.  Reconcile
  # removal before permitting cleanup of either bind-mount directory.
  PREFLIGHT_DIRS_SAFE_TO_DELETE=0
  if ! remove_container_and_verify "$preflight_name"; then
    echo "Preflight container removal was not verified; preserving temporary bind-mount directories." >&2
    return 1
  fi
  PREFLIGHT_DIRS_SAFE_TO_DELETE=1
  clear_preflight_dirs
  if [ "$healthy" != "1" ]; then
    echo "Release image failed isolated UID/GID, mount, import, or route preflight." >&2
    return 1
  fi
  echo "${preflight_label^^}_IMAGE_PREFLIGHT_OK image=$image_tag"
}

preflight_release_image() {
  preflight_image "$release_tag" release
}

wait_for_local_health() {
  for attempt in $(seq 1 20); do
    if curl "${CURL_BOUNDED[@]}" -fsS http://127.0.0.1:9091/api/health >/dev/null; then
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
  preflight_image "$rollback_tag" rollback
}

rollback_live() {
  local status="${1:-$?}"
  local inspect_status=0
  local fallback_status=0
  if [ "$status" -eq 0 ]; then
    return 0
  fi
  echo ""
  echo "=== Deploy failed; attempting rollback ===" >&2
  if [ "$replacement_started" = "1" ] && [ "$container_replaced" != "1" ]; then
    if reconcile_replacement; then
      :
    else
      inspect_status=$?
      if [ "$inspect_status" -eq 2 ]; then
        echo "Replacement container existence is uncertain; refusing rollback mutation." >&2
        exit "$status"
      fi
    fi
  fi
  if [ "$removal_started" = "1" ] && [ "$removal_confirmed" != "1" ]; then
    if container_inspect_state "$APP_NAME"; then
      echo "Container removal outcome is uncertain because the existing container is still present; refusing rollback mutation." >&2
      exit "$status"
    else
      inspect_status=$?
    fi
    case "$inspect_status" in
      1) removal_confirmed=1 ;;
      *)
        echo "Container removal outcome is uncertain because Docker inspect failed without confirming absence; refusing rollback mutation." >&2
        exit "$status"
        ;;
    esac
  fi
  if [ "$container_replaced" != "1" ] && [ "$removal_confirmed" != "1" ]; then
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
  if ! remove_container_and_verify "$APP_NAME"; then
    echo "Rollback could not verify removal of the replacement container; refusing to start a fallback container." >&2
    exit "$status"
  fi
  removal_confirmed=1
  if start_container_and_reconcile "$rollback_tag"; then
    :
  else
    fallback_status=$?
    if [ "$fallback_status" -eq 2 ]; then
      echo "Rollback container existence is uncertain; refusing further mutation." >&2
    else
      echo "Rollback container could not be started from $rollback_tag." >&2
    fi
    exit "$status"
  fi
  for attempt in $(seq 1 20); do
    if curl "${CURL_BOUNDED[@]}" -fsS http://127.0.0.1:9091/api/health >/dev/null \
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

rollback_signal() {
  local status=$?
  [ "$status" -eq 0 ] && status=1
  if [ -n "$active_child_pid" ]; then
    kill -TERM "$active_child_pid" >/dev/null 2>&1 || true
    wait "$active_child_pid" >/dev/null 2>&1 || true
    active_child_pid=""
  fi
  rollback_live "$status"
}

trap cleanup EXIT
trap rollback_live ERR
trap rollback_signal HUP INT TERM

echo "=== Building Solo Studio Video ==="
if [ -L "$APP_DIR" ] || [ ! -d "$APP_DIR" ]; then
  echo "Application directory must be a real directory before build." >&2
  exit 1
fi
cd "$APP_DIR"
timestamp=$(date -u +%Y%m%d%H%M%S%N)-$$
release_tag="solo-studio-video:release-$timestamp"
rollback_tag="solo-studio-video:rollback-$timestamp"

current_image=""
current_container_state=0
if container_inspect_state "$APP_NAME"; then
  existing_container_found=1
  if ! current_image=$(docker container inspect -f '{{.Image}}' "$APP_NAME" 2>/dev/null); then
    echo "Docker reported the existing container but could not inspect its image; refusing deployment." >&2
    exit 1
  fi
else
  current_container_state=$?
  case "$current_container_state" in
    1) ;;
    *)
      echo "Could not determine whether $APP_NAME exists because Docker inspect failed; refusing deployment." >&2
      exit 1
      ;;
  esac
fi
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

run_tracked docker build -t "$release_tag" .
run_tracked docker tag "$release_tag" "$APP_NAME:latest"

# Ensure shared state file exists with correct host/container permissions.
if [ -L "$APP_DIR" ] || [ ! -d "$APP_DIR" ]; then
  echo "Application directory must be a real directory." >&2
  exit 1
fi
for shared_dir in "$APP_DIR/state" "$APP_DIR/output"; do
  if [ -L "$shared_dir" ] || { [ -e "$shared_dir" ] && [ ! -d "$shared_dir" ]; }; then
    echo "Refusing symlinked or non-directory bind-mount path: $shared_dir" >&2
    exit 1
  fi
done
mkdir -p "$APP_DIR/state"
mkdir -p "$APP_DIR/output"
if [ -L "$APP_DIR/state/jobs.json" ]; then
  echo "Refusing symlinked state/jobs.json; repair the state directory before deployment." >&2
  exit 1
fi
if [ ! -e "$APP_DIR/state/jobs.json" ]; then
  if [ -L "$APP_DIR/jobs.json" ]; then
    echo "Refusing symlinked legacy jobs.json migration source." >&2
    exit 1
  elif [ -f "$APP_DIR/jobs.json" ]; then
    copy_regular_file_excl "$APP_DIR/jobs.json" "$APP_DIR/state/jobs.json" 660
  elif [ -e "$APP_DIR/jobs.json" ]; then
    echo "Refusing non-regular legacy jobs.json migration source." >&2
    exit 1
  else
    printf '{}\n' > "$APP_DIR/state/jobs.json"
  fi
fi
chmod 660 "$APP_DIR/state/jobs.json"
if [ "$(id -u)" = "0" ]; then
  chown -R 10001:10001 "$APP_DIR/state" "$APP_DIR/output"
  if [ "$SOLO_STUDIO_API_TOKEN_EPHEMERAL" = "1" ]; then
    chown 10001:10001 "$SOLO_STUDIO_API_TOKEN_FILE"
  fi
  CONTAINER_USER_ARGS=(--user 10001:10001)
  runtime_uid=10001
  runtime_gid=10001
else
  CONTAINER_USER_ARGS=(--user "$(id -u):$(id -g)")
  runtime_uid=$(id -u)
  runtime_gid=$(id -g)
  if [ ! -w "$APP_DIR/state" ] || [ ! -w "$APP_DIR/output" ]; then
    echo "Current host identity $(id -u):$(id -g) cannot write the state/output bind mounts." >&2
    exit 1
  fi
fi

if [ "$(stat -c '%u' "$SOLO_STUDIO_API_TOKEN_FILE")" != "$runtime_uid" ]; then
  echo "SOLO_STUDIO_API_TOKEN_FILE must be owned by the selected container UID ($runtime_uid)." >&2
  exit 1
fi

if [ "$SOLO_STUDIO_ENABLE_HIGGSFIELD" = "1" ]; then
  if ! run_tracked docker run --rm --network none --user "$runtime_uid:$runtime_gid" \
    -v "$HIGGSFIELD_CREDENTIALS_FILE:/mnt/higgsfield-credentials:ro" \
    "$release_tag" sh -ceu 'test -r /mnt/higgsfield-credentials'; then
    echo "Higgsfield credentials are not readable by the selected container UID/GID." >&2
    exit 1
  fi
fi

if ! run_tracked docker run --rm --network none --user "$runtime_uid:$runtime_gid" \
  -v "$APP_DIR/state:/mnt/state" \
  -v "$APP_DIR/output:/mnt/output" \
  "$release_tag" sh -ceu '
    test -w /mnt/state && test -w /mnt/output
    for path in /mnt/state/jobs.json /mnt/state/solo_studio.sqlite3 /mnt/state/solo_studio.sqlite3-wal /mnt/state/solo_studio.sqlite3-shm; do
      if [ -e "$path" ] && [ ! -w "$path" ]; then exit 1; fi
    done
    touch /mnt/state/.solo-studio-write-test /mnt/output/.solo-studio-write-test
    rm -f /mnt/state/.solo-studio-write-test /mnt/output/.solo-studio-write-test
  '; then
  echo "State/output bind mounts are not writable by the selected container UID/GID." >&2
  exit 1
fi

preflight_release_image

curl_config_dir=$(mktemp -d)
chmod 700 "$curl_config_dir"
curl_config="$curl_config_dir/config"
if ! python3 -c 'import os, pathlib, stat, sys
token_fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
try:
    token_stat = os.fstat(token_fd)
    if not stat.S_ISREG(token_stat.st_mode) or token_stat.st_uid != int(sys.argv[3]) or token_stat.st_mode & 0o77:
        raise SystemExit("invalid API token file metadata")
    token = os.read(token_fd, 4097).decode("utf-8")
finally:
    os.close(token_fd)
if len(token) > 4096:
    raise SystemExit("API token is too long")
if token.endswith("\n"):
    token = token[:-1]
if not token or any(ord(character) < 0x20 or ord(character) == 0x7F or ord(character) in (34, 92) for character in token):
    raise SystemExit("invalid API token file contents")
config = ("header = " + chr(34) + "Authorization: Bearer " + token + chr(34) + chr(10)).encode("utf-8")
config_fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
try:
    written = os.write(config_fd, config)
    if written != len(config):
        raise SystemExit("short curl config write")
    os.fsync(config_fd)
finally:
    os.close(config_fd)' \
  "$SOLO_STUDIO_API_TOKEN_FILE" "$curl_config" "$runtime_uid"; then
  echo "Could not create the authenticated curl configuration from SOLO_STUDIO_API_TOKEN_FILE." >&2
  exit 1
fi

echo ""
echo "=== Deploying on edgescout.tech/video ==="
container_replaced=0
removal_started=0
removal_confirmed=0
replacement_started=0
existing_container_status=""
if container_inspect_state "$APP_NAME"; then
  existing_container_found=1
  if ! existing_container_status=$(docker container inspect -f '{{.State.Status}}' "$APP_NAME" 2>/dev/null); then
    echo "Docker reported the existing container but could not inspect its runtime status; refusing deployment." >&2
    exit 1
  fi
  case "$existing_container_status" in
    running)
      if ! curl "${CURL_BOUNDED[@]}" -fsS --config "$curl_config" "$PUBLIC_JOBS_URL" >/dev/null; then
        echo "Existing running container failed the authenticated protected-route preflight; refusing replacement." >&2
        exit 1
      fi
      ;;
    restarting|exited|created|dead|paused)
      echo "Existing container is unavailable (status=$existing_container_status); replacing only after release preflight." >&2
      ;;
    *)
      echo "Existing container has an unrecognized runtime status ($existing_container_status); refusing deployment." >&2
      exit 1
      ;;
  esac
  removal_started=1
  if ! remove_container_and_verify "$APP_NAME"; then
    echo "Existing container removal was not verified; refusing replacement." >&2
    exit 1
  fi
  removal_confirmed=1
else
  current_container_state=$?
  case "$current_container_state" in
    1) ;;
    *)
      echo "Could not determine whether $APP_NAME exists before replacement; refusing deployment." >&2
      exit 1
      ;;
  esac
fi
if [ "$existing_container_found" = "1" ] && [ "$removal_confirmed" != "1" ]; then
  echo "Existing container could not be removed; refusing replacement." >&2
  exit 1
fi
start_status=0
if start_container_and_reconcile "$release_tag"; then
  :
else
  start_status=$?
fi
if [ "$start_status" -ne 0 ]; then
  rollback_live 1
fi

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

echo ""
echo "=== Public Traefik smoke test ==="
wait_for_public_health
curl "${CURL_BOUNDED[@]}" -fsS "$DOMAIN/api/health"

curl "${CURL_BOUNDED[@]}" -sSI "$DOMAIN/" 2>&1 | head -5
curl "${CURL_BOUNDED[@]}" -fsS "$DOMAIN/api/health" 2>&1
curl "${CURL_BOUNDED[@]}" -fsS --config "$curl_config" "$PUBLIC_JOBS_URL" >/dev/null

echo ""
echo "=== URL ==="
echo "  $DOMAIN/"
echo ""
echo "Done."
