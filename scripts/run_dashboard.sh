#!/usr/bin/env bash
# Launch the local Calibr dashboard stack (read-only API + React development UI).
# Press Ctrl-C to stop both processes started by this script.

set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$project_root/.venv/bin/python"
web_dir="$project_root/dashboard/web"

if [[ ! -x "$python_bin" ]]; then
  echo "Missing virtual environment at .venv. Create it and install the Python dependencies first." >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required for the React dashboard. Install Node.js, then run this script again." >&2
  exit 1
fi

if [[ ! -d "$web_dir/node_modules" ]]; then
  echo "Dashboard JavaScript dependencies are missing. Run: cd dashboard/web && npm install" >&2
  exit 1
fi

pick_port() {
  local port="$1"
  while lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; do
    port=$((port + 1))
  done
  printf '%s' "$port"
}

api_port="$(pick_port "${CALIBR_API_PORT:-8000}")"
web_port="$(pick_port "${CALIBR_WEB_PORT:-5173}")"
api_pid=""
web_pid=""

cleanup() {
  [[ -n "$web_pid" ]] && kill "$web_pid" 2>/dev/null || true
  [[ -n "$api_pid" ]] && kill "$api_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$project_root"
"$python_bin" -m dashboard.server --port "$api_port" &
api_pid=$!

# Confirm the API can both bind and query Postgres before starting the UI.
for _ in {1..25}; do
  if curl --fail --silent "http://127.0.0.1:${api_port}/api/overview" >/dev/null; then
    break
  fi
  if ! kill -0 "$api_pid" 2>/dev/null; then
    echo "Dashboard API stopped during startup." >&2
    exit 1
  fi
  sleep 0.2
done

if ! curl --fail --silent "http://127.0.0.1:${api_port}/api/overview" >/dev/null; then
  echo "Dashboard API could not query Postgres. Start the database, then try again." >&2
  exit 1
fi

echo "Calibr dashboard: http://127.0.0.1:${web_port}"
echo "API: http://127.0.0.1:${api_port} (Ctrl-C stops both)"
cd "$web_dir"
CALIBR_API_PORT="$api_port" npm run dev -- --host 127.0.0.1 --port "$web_port" &
web_pid=$!
wait "$web_pid"
