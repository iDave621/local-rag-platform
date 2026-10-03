#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
python_bin="${PYTHON_BIN:-python3}"
if command -v "$python_bin" >/dev/null 2>&1 && "$python_bin" --version >/dev/null 2>&1; then
  "$python_bin" server/scripts/offline_tool.py init-env --root .
else
  docker run --rm --pull never --network none -v "$PWD:/release" local-rag/backend:2.2.0 python /release/server/scripts/offline_tool.py init-env --root /release
fi
