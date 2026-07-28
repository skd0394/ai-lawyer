#!/usr/bin/env bash
BASE="https://docdraft-developers-kartik--ailaw-kartik-fastapi-app.modal.run"

echo "=== clear any stale lock ==="
curl -s -X POST "$BASE/api/turn/force-release?user_id=kartik"; echo

echo "=== start long turn in background ==="
curl -s -X POST "$BASE/api/debug/loop" -H 'Content-Type: application/json' \
  -d '{"prompt":"Call wait_tool, then call it again, then again, then again. One at a time.","max_iterations":8}' \
  > /tmp/cancel_turn.json &
TURN_PID=$!

sleep 5

echo "=== status (expect running:true) ==="
curl -s "$BASE/api/turn/status?user_id=kartik"; echo

echo "=== double submit (expect HTTP 409) ==="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/api/debug/loop" \
  -H 'Content-Type: application/json' -d '{"prompt":"hello"}'

echo "=== cancel ==="
curl -s -X POST "$BASE/api/turn/cancel?user_id=kartik"; echo

wait $TURN_PID

echo "=== result ==="
python3 -c "
import json
d = json.load(open('/tmp/cancel_turn.json'))
last = d['events'][-1]
print('stop_reason      :', last['stop_reason'])
print('total_ms         :', last['total_ms'])
print('message_count    :', d['message_count'])
print('session_integrity:', d['session_integrity'])
"

echo "=== status after (expect running:false) ==="
curl -s "$BASE/api/turn/status?user_id=kartik"; echo
