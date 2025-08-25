#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
echo "== Engine dir: $(pwd)"

# Optionally load .env (lets you override GSHEET_URL, etc.)
if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
  [[ -n "${GSHEET_URL:-}" ]] && echo "== GSHEET_URL set"
  [[ -n "${GOOGLE_API_KEY:-}" ]] && echo "== GOOGLE_API_KEY present"
fi

# Create a virtual environment if missing
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ ! -d ".venv" ]]; then
  echo "== Creating virtualenv (.venv)..."
  "$PYTHON_BIN" -m venv .venv
fi

# Activate it
# shellcheck disable=SC1091
source .venv/bin/activate

# Install/upgrade minimal deps
echo "== Checking/Installing Python deps (pandas, openpyxl)..."
python - <<'PY'
import sys, subprocess
pkgs = ["pandas","openpyxl"]
subprocess.check_call([sys.executable,"-m","pip","install","--upgrade","--quiet","pip","wheel"])
subprocess.check_call([sys.executable,"-m","pip","install","--quiet"] + pkgs)
print("== Deps OK")
PY

echo "== Running generator (Google Sheets mode)..."
python generate_index.py

status=$?
if [[ $status -ne 0 ]]; then
  echo "== Generator exited with status $status"
  exit $status
fi

# Auto-open the file on macOS
if [[ -f "index.html" ]] && command -v open >/dev/null 2>&1; then
  open index.html
fi

echo "== Done."
