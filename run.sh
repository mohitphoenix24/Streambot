#!/usr/bin/env bash
# Launch the whole app: FastAPI backend + Vite frontend. Ctrl+C stops both.
#
#   ./run.sh           uses OpenAI with the key and model from .env
#
# Options:  --backend-port N (default 8501)   --frontend-port N (default 3100;
#           Vite moves to the next free port if it's taken and prints the URL)
#
# Starting the app makes NO OpenAI request. Only sending a chat message does.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_PORT=8501
FRONTEND_PORT=3100

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-port) BACKEND_PORT="$2"; shift ;;
    --frontend-port) FRONTEND_PORT="$2"; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 2 ;;
  esac
  shift
done

die() { echo "ERROR: $*" >&2; exit 1; }
port_in_use() { ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"; }

# ── 1. one-time setup ────────────────────────────────────────────────────
command -v node >/dev/null || die "Node.js is not installed."
command -v python3 >/dev/null || die "python3 is not installed."

if [[ ! -x "$ROOT/AI/.venv/bin/python" ]]; then
  echo "▶ Creating AI virtualenv and installing dependencies..."
  python3 -m venv "$ROOT/AI/.venv"
  "$ROOT/AI/.venv/bin/pip" install -q -r "$ROOT/AI/requirements.txt"
fi
if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
  echo "▶ Installing frontend dependencies..."
  (cd "$ROOT/frontend" && npm install --no-audit --no-fund)
fi

port_in_use "$BACKEND_PORT" && die "Port $BACKEND_PORT is already in use. Use --backend-port N."

# ── 2. configuration check (fails early with an actionable message) ──────
[[ -f "$ROOT/.env" ]] || die ".env not found. Run: cp .env.example .env  and set OPENAI_API_KEY and OPENAI_MODEL."
(cd "$ROOT/AI" && ./.venv/bin/python - <<'PY'
import sys
from app.config import ConfigError, Settings
from app.llm.pricing import PriceTable

s = Settings.from_env()
try:
    s.validate()
except ConfigError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(1)
table = PriceTable.load(s.prices_path)
for name in {s.openai_model, s.title_model}:
    if table.get(name) is None:
        print(f"NOTE: no price for {name!r} in AI/data/prices.toml, so costs will show as 'cost n/a'.")
PY
) || exit 1

# ── 3. start both servers; Ctrl+C stops everything ──────────────────────
set -m   # job control: each background job gets its own process group
PIDS=()
cleanup() {
  trap - INT TERM EXIT
  echo; echo "▶ Stopping..."
  for pid in "${PIDS[@]:-}"; do [[ -n "$pid" ]] && kill -- "-$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "▶ Model    : OpenAI, from .env. Every chat message spends money."
echo "▶ Backend  : http://localhost:$BACKEND_PORT   (API docs: /docs)"

(cd "$ROOT/AI" && exec ./.venv/bin/python -m uvicorn app.main:app --reload --port "$BACKEND_PORT" 2>&1 | sed -u 's/^/[backend]  /') &
PIDS+=($!)

(cd "$ROOT/frontend" && VITE_API_URL="http://localhost:$BACKEND_PORT" exec npm run dev -- --port "$FRONTEND_PORT" 2>&1 | sed -u 's/^/[frontend] /') &
PIDS+=($!)

echo "▶ Frontend : open the 'Local:' URL printed below (Vite picks another port if $FRONTEND_PORT is busy)"
echo "▶ Press Ctrl+C to stop."
wait
