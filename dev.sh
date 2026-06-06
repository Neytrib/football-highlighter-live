#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

CONTAINER_NAME="${ACESTREAM_CONTAINER_NAME:-football-acestream}"
ACESTREAM_IMAGE="${ACESTREAM_IMAGE:-blaiseio/acelink}"
ENGINE_URL="${ACESTREAM_ENGINE_URL:-http://127.0.0.1:6878/webui/api/service?method=get_version}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
CONFIG_PATH="${CONFIG_PATH:-configs/config.yaml}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"
LOG_FILE="${LOG_FILE:-data/state/runtime.log}"
DRY_RUN="${DRY_RUN:-0}"
UI_HOST="${UI_HOST:-127.0.0.1}"
UI_PORT="${UI_PORT:-5174}"
CLEAN_STALE_PROCESSES="${CLEAN_STALE_PROCESSES:-1}"

ENGINE_ONLY=0
if [[ "${1:-}" == "--engine-only" ]]; then
  ENGINE_ONLY=1
  shift
fi

create_engine_container() {
  docker run -d \
    --name "$CONTAINER_NAME" \
    --platform=linux/amd64 \
    -p 6878:6878 \
    -p 8621:8621 \
    -p 8621:8621/udp \
    "$ACESTREAM_IMAGE" >/dev/null
}

container_has_required_ports() {
  local ports
  ports="$(docker inspect -f '{{json .NetworkSettings.Ports}}' "$CONTAINER_NAME" 2>/dev/null || true)"
  [[ "$ports" == *'"6878/tcp":[{'* ]] \
    && [[ "$ports" == *'"8621/tcp":[{'* ]] \
    && [[ "$ports" == *'"8621/udp":[{'* ]]
}

stop_stale_processes() {
  if [[ "$CLEAN_STALE_PROCESSES" == "0" ]]; then
    return
  fi

  local patterns=(
    "app\\.ui\\.server --config $CONFIG_PATH"
    "app\\.main --config $CONFIG_PATH .*--log-file $LOG_FILE"
    "ffmpeg .*pid=football-highlighter-recorder.*data/tmp"
  )

  local pattern
  local pids
  for pattern in "${patterns[@]}"; do
    pids="$(pgrep -f "$pattern" || true)"
    if [[ -n "$pids" ]]; then
      echo "Stopping stale football highlighter process(es): $pids"
      pkill -TERM -f "$pattern" || true
    fi
  done

  sleep 1

  for pattern in "${patterns[@]}"; do
    pids="$(pgrep -f "$pattern" || true)"
    if [[ -n "$pids" ]]; then
      echo "Force-stopping stale football highlighter process(es): $pids"
      pkill -KILL -f "$pattern" || true
    fi
  done
}

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required to run the AceStream engine" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  if command -v colima >/dev/null 2>&1; then
    echo "Starting Colima..."
    colima start
  else
    echo "Docker is not running, and colima is not installed" >&2
    exit 1
  fi
fi

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  if ! container_has_required_ports; then
    echo "Recreating AceStream engine container with HTTP and P2P ports..."
    docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker rm "$CONTAINER_NAME" >/dev/null
    create_engine_container
  elif [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" != "true" ]]; then
    echo "Starting AceStream engine container..."
    docker start "$CONTAINER_NAME" >/dev/null
  fi
else
  echo "Creating AceStream engine container..."
  create_engine_container
fi

echo "Waiting for AceStream engine..."
for _ in {1..30}; do
  if curl -fsS "$ENGINE_URL" >/dev/null 2>&1; then
    echo "AceStream engine is ready at http://127.0.0.1:6878"
    if [[ "$ENGINE_ONLY" == "1" ]]; then
      exit 0
    fi
    break
  fi
  sleep 1
done

if ! curl -fsS "$ENGINE_URL" >/dev/null 2>&1; then
  echo "AceStream engine did not become ready at $ENGINE_URL" >&2
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python virtualenv not found at $PYTHON_BIN" >&2
  echo "Run: /opt/homebrew/bin/python3.11 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p data/state data/tmp
export MPLCONFIGDIR="${MPLCONFIGDIR:-$ROOT_DIR/data/tmp/matplotlib}"
mkdir -p "$MPLCONFIGDIR"

APP_ARGS=(
  -m app.ui.server
  --config "$CONFIG_PATH"
  --log-level "$LOG_LEVEL"
  --log-file "$LOG_FILE"
  --host "$UI_HOST"
  --port "$UI_PORT"
  --engine-url "$ENGINE_URL"
  --engine-container "$CONTAINER_NAME"
  --engine-image "$ACESTREAM_IMAGE"
)
if [[ "$DRY_RUN" != "0" ]]; then
  APP_ARGS+=(--dry-run)
else
  APP_ARGS+=(--live-clips)
fi

echo "Starting football highlighter UI..."
stop_stale_processes
exec "$PYTHON_BIN" "${APP_ARGS[@]}" "$@"
