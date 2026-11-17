#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

export DATABASE_URL="${DATABASE_URL:-sqlite:///$ROOT_DIR/data/sovereign.db}"
export MCP_API_KEY="${MCP_API_KEY:-dev-mcp-internal-key}"
export ADMIN_API_KEY="${ADMIN_API_KEY:-dev-admin-secret-key}"
export ERP_MCP_URL="${ERP_MCP_URL:-http://127.0.0.1:8101/mcp}"
export ERP_ADMIN_URL="${ERP_ADMIN_URL:-http://127.0.0.1:8101}"
export KNOWLEDGE_MCP_URL="${KNOWLEDGE_MCP_URL:-http://127.0.0.1:8102/mcp}"
export GATEWAY_HOST="${GATEWAY_HOST:-127.0.0.1}"
export GATEWAY_PORT="${GATEWAY_PORT:-8000}"
if [[ -z "${LLM_MODE:-}" ]]; then
  if [[ -f "${GOOGLE_APPLICATION_CREDENTIALS:-$HOME/.config/gcloud/application_default_credentials.json}" ]]; then
    export LLM_MODE="vertex_adc"
  else
    export LLM_MODE="offline"
  fi
fi
export PRIMARY_MODEL="${PRIMARY_MODEL:-gemini-3.8-flash}"
export FALLBACK_MODEL="${FALLBACK_MODEL:-gemini-3.5-flash-lite}"
export VERTEX_PROJECT_ID="${VERTEX_PROJECT_ID:-gen-lang-client-0430700811}"
export VERTEX_LOCATION="${VERTEX_LOCATION:-global}"
export PYTHON_EXECUTABLE="${PYTHON_EXECUTABLE:-$ROOT_DIR/.venv/bin/python}"

mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/data/traces" "$ROOT_DIR/evals/reports"

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[start_local] Seeding database..."
uv run python -m dataset.seed

echo "[start_local] Starting mcp_erp on :8101..."
uv run python -m services.mcp_erp.server &
PIDS+=($!)

echo "[start_local] Starting mcp_knowledge on :8102..."
uv run python -m services.mcp_knowledge.server &
PIDS+=($!)

for port in 8101 8102; do
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
done

echo "[start_local] Starting gateway on ${GATEWAY_HOST}:${GATEWAY_PORT}..."
echo "[start_local] Ops Console -> http://${GATEWAY_HOST}:${GATEWAY_PORT}/"
uv run uvicorn services.gateway.app:app --host "$GATEWAY_HOST" --port "$GATEWAY_PORT" &
PIDS+=($!)

wait "${PIDS[@]}"
