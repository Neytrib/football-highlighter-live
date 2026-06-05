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
DRY_RUN="${DRY_RUN:-1}"

ENGINE_ONLY=0
if [[ "${1:-}" == "--engine-only" ]]; then
  ENGINE_ONLY=1
  shift
fi

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
  if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" != "true" ]]; then
    echo "Starting AceStream engine container..."
    docker start "$CONTAINER_NAME" >/dev/null
  fi
else
  echo "Creating AceStream engine container..."
  docker run -d \
    --name "$CONTAINER_NAME" \
    --platform=linux/amd64 \
    -p 6878:6878 \
    "$ACESTREAM_IMAGE" >/dev/null
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

APP_ARGS=(-m app.main --config "$CONFIG_PATH" --log-level "$LOG_LEVEL")
if [[ "$DRY_RUN" != "0" ]]; then
  APP_ARGS+=(--dry-run)
fi

echo "Starting football highlighter..."
exec "$PYTHON_BIN" "${APP_ARGS[@]}" "$@"
