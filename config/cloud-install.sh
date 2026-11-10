#!/usr/bin/env bash
set -euo pipefail
cd /workspace/Memory
export UV_CACHE_DIR=/workspace/.cache/uv
export UV_PYTHON_DOWNLOADS=never
python3 -c 'import sys; assert sys.version_info >= (3, 12)'
node -e 'if (Number(process.versions.node.split(".")[0]) < 24) process.exit(1)'
uv sync --frozen
.venv/bin/pytest -q
for test_file in vendor/hstry/extension/test/*.js; do
  node "$test_file"
done
.venv/bin/ai-memory demo --output data/demo
.venv/bin/python scripts/mcp_smoke.py --db data/demo/conversations.sqlite
