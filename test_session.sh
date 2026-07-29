#!/usr/bin/env bash
# T3.1 — session persistence and context reconstruction.
#
# Proves the spec requirement:
#   "A session started on turn 1 should reconstruct full context on turn N,
#    including after the sandbox was recycled."
#
# Usage:  ./test_session.sh

BASE="https://docdraft-developers-kartik--ailaw-kartik-fastapi-app.modal.run"
USER="kartik"
S="sess-$(date +%s)"

echo "session id: $S"
echo

# Say something, print the assistant's reply plus the turn stats.
say () {
  local prompt="$1"
  local body
  body=$(python3 -c 'import json,sys; print(json.dumps({"prompt":sys.argv[1],"session_id":sys.argv[2]}))' "$prompt" "$S")

  # -o to a file, NOT a pipe. Piping to head/grep closes the pipe early,
  # curl dies on SIGPIPE, and you see nothing.
  curl -s -X POST "$BASE/api/chat" \
       -H 'Content-Type: application/json' \
       -d "$body" -o /tmp/sse.txt

  python3 - <<'PY'
import json
text = ""
for line in open('/tmp/sse.txt'):
    if not line.startswith('data: '):
        continue
    try:
        ev = json.loads(line[6:])
    except Exception:
        continue
    if ev.get('type') == 'text_delta':
        text += ev['text']
    elif ev.get('type') == 'tool_end':
        print(f"    [tool] {ev['name']} ok={ev['ok']} {ev['ms']}ms")
    elif ev.get('type') == 'error':
        print(f"    [ERROR] {ev['code']}: {ev['message']}")
    elif ev.get('type') == 'turn_end':
        print("  " + (text.strip()[:400] or "(no text)"))
        print(f"    -> {ev['stop_reason']} | in {ev['billed_input_tokens']}"
              f" | out {ev['billed_output_tokens']} | ${ev['cost_usd']}"
              f" | {ev['total_ms']}ms")
PY
}

# Clear any lock left over from an interrupted run.
curl -s -X POST "$BASE/api/turn/force-release?user_id=$USER" > /dev/null

echo "=== TURN 1: give it a fact ==="
say "My name is Kartik and I own a commercial building at 400 W Madison in Chicago."
echo

echo "=== TURN 2: does it remember? ==="
say "What is my name and where is my property?"
echo

echo "=== destroying the sandbox ==="
curl -s -X DELETE "$BASE/api/debug/sandbox?user_id=$USER"; echo
echo

echo "=== TURN 3: after sandbox death — THE actual requirement ==="
say "Remind me what we discussed about my building."
echo

echo "=== transcript ==="
curl -s "$BASE/api/sessions/$S/transcript" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f\"turns={d['turn_count']}  messages={d['message_count']}  context_chars={d['context_chars']}\")
for item in d['transcript']:
    tag = 'USER' if item['role'] == 'user' else 'ASST'
    tools = item.get('tools_used') or []
    extra = f\"  tools={tools}\" if tools else ''
    print(f\"  {tag}: {item['text'][:110]}{extra}\")
"
echo

echo "=== session integrity ==="
curl -s "$BASE/api/sessions/$S/messages" | python3 -c "
import json, sys
print(json.load(sys.stdin)['integrity'])
"
