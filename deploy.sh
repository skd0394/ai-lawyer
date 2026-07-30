#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
BASE="https://docdraft-developers-kartik--ailaw-kartik-fastapi-app.modal.run"

echo "--- syntax check (python 3.11 rules) ---"
python3 -c "
import ast, pathlib, sys
bad = []
for f in sorted(pathlib.Path('.').rglob('*.py')):
    if '.venv' in str(f) or 'node_modules' in str(f):
        continue
    try:
        ast.parse(f.read_text(), feature_version=(3, 11))
    except SyntaxError as e:
        bad.append(f'  {f}:{e.lineno}  {e.msg}')
if bad:
    print('SYNTAX ERRORS — not deploying:')
    print('\n'.join(bad))
    sys.exit(1)
print('ok')
"

echo ""
echo "--- local files ---"
for f in modal_app.py agents/agent_b/state.py harness/loop.py sandbox/worker.py; do
  [ -f "$f" ] && printf '  %-32s %5s lines  %s\n' "$f" "$(wc -l < "$f")" "$(md5 -q "$f" | cut -c1-8)"
done

modal deploy modal_app.py

echo ""
echo "BASE=$BASE"
echo "--- live routes ---"
curl -s "$BASE/openapi.json" \
  | python3 -c "import json,sys; p=json.load(sys.stdin)['paths']; print(*p, sep='\n'); print('TOTAL:', len(p))"
