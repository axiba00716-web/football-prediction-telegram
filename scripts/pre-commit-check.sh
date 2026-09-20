#!/bin/bash
set -euo pipefail

echo ">>> python -m compileall app"
python -m compileall app

echo ">>> pytest -q"
pytest -q

echo ">>> git diff --check"
git diff --check

echo "All checks passed."
