#!/usr/bin/env bash
# Run from the project root: ./runner.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

git pull
source venv/bin/activate
python genetic.py
