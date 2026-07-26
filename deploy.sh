#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
BASE="https://docdraft-developers-kartik--ailaw-kartik-fastapi-app.modal.run"

modal deploy modal_app.py

echo ""
echo "BASE=$BASE"
echo "--- live routes ---"
curl -s "$BASE/openapi.json" \
  | python3 -c "import json,sys; print(*json.load(sys.stdin)['paths'], sep='\n')"
