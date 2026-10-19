#!/usr/bin/env bash
# Run the pipeline locally.
# Usage: bash run_local.sh [--dry-run]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source .venv/bin/activate

echo "[INFO] Starting the job hunt pipeline..."
python main.py "$@"
