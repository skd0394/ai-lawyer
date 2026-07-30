#!/usr/bin/env python3
"""Agent B consultation driver.

    python3 consult.py new     "my partner wants to leave the business"
    python3 consult.py answer  q_state=Illinois q_entity=LLC "q_agr=No written agreement"
    python3 consult.py skip    "operating agreement"
    python3 consult.py upload  /tmp/lease.docx
    python3 consult.py say     "anything else?"
    python3 consult.py state
    python3 consult.py raw

Answers are separate argv entries, so shell quoting only has to survive
normal argument passing. Set SESSION=other to use a different session.
"""

import json
import os
import subprocess
import sys

BASE = os.environ.get(
    "BASE",
    "https://docdraft-developers-kartik--ailaw-kartik-fastapi-app.modal.run")
USER = os.environ.get("USER_ID", "kartik")
SESSION = os.environ.get("SESSION", "consult-t1")
SSE_PATH = "/tmp/consult_sse.txt"


def curl(args):
    return subprocess.run(["curl", "-s"] + args, capture_output=True,
                          text=True).stdout


def post_json(path, body, out=None):
    args = ["-X", "POST", f"{BASE}{path}",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(body)]
    if out:
        args += ["-o", out]
    return curl(args)


def render(path=SSE_PATH):
    try:
        lines = open(path).read().splitlines()
    except FileNotFoundError:
        print("no response file")
        return

    if not any(l.startswith("data: ") for l in lines):
        print("NO SSE FRAMES. Raw response:")
        print("\n".join(lines)[:900])
        return

    text = ""
    for l in lines:
        if not l.startswith("data: "):
            continue
        try:
            e = json.loads(l[6:])
        except Exception:
            continue
        ty = e.get("type")

        if ty == "tool_start":
            print(f"  [start] {e['name']} {e['args_preview'][:80]}")
        elif ty == "tool_end":
            print(f"  [end  ] {e['name']} "
                  f"{'ok' if e['ok'] else 'FAIL'} {e['summary'][:70]}")
        elif ty == "citation":
            print(f"  [cite ] {e['confidence'].upper()} {e['url'][:70]}")
        elif ty == "error":
            print(f"  [ERROR] {e['code']}: {e['message'][:220]}")
        elif ty == "text_delta":
            text += e["text"]
        elif ty == "structured_block":
            kind, p = e["kind"], e["payload"]
            if kind == "question_form":
                print("\n  === QUESTION FORM ===")
                if p.get("context"):
                    print("  " + p["context"])
                for q in p.get("questions", []):
                    opt = "" if q.get("required", True) else "  (optional)"
                    print(f"    [{q['type']}] {q['prompt']}{opt}")
                    if q.get("options"):
                        print(f"        options: {q['options']}")
                    print(f"        why: {q['help_text'][:130]}")
                    print(f"        id:  {q['id']}")
            elif kind == "file_request":
                print("\n  === FILE REQUEST ===")
                print(f"    document: {p.get('document_name')}")
                print(f"    reason:   {p.get('reason', '')[:180]}")
                if p.get("alternative_if_unavailable"):
                    print(f"    if you skip: "
                          f"{p['alternative_if_unavailable'][:150]}")
            elif kind == "findings":
                print("\n  === FINDINGS ===")
                for f in p.get("findings", []):
                    print(f"    [{f.get('severity', 'info')}] {f.get('title')}")
                    print(f"        {f.get('summary', '')[:170]}")
                    for src in f.get("sources", [])[:3]:
                        print(f"        src: {src.get('confidence', '?')} "
                              f"{str(src.get('url', ''))[:70]}")
            elif kind in ("drafting_handoff", "attorney_conclusion"):
                print(f"\n  === TERMINAL: {kind.upper()} ===")
                print(json.dumps(p, indent=4)[:2000])
            elif kind == "document":
                print(f"\n  [DOC] {p.get('filename')} "
                      f"({p.get('word_count')} words)")
            elif kind == "consultation_state":
                print(f"\n  STATE: phase={p['phase']} "
                      f"pending={len(p['pending_questions'])} "
                      f"collected={p['collected_count']} "
                      f"terminal={p['is_terminal']}")
                print(f"  tools: {', '.join(p['allowed_tools']) or '(none)'}")
        elif ty == "turn_end":
            print()
            print(f"  prose: {text.strip()[:300]!r}" if text.strip()
                  else "  prose: (none)")
            print(f"  -> {e['stop_reason']} | in {e['billed_input_tokens']} "
                  f"| out {e['billed_output_tokens']} | ${e['cost_usd']} "
                  f"| {e['total_ms']}ms")


def send(prompt="", answers=None, skip_file=""):
    body = {"prompt": prompt, "session_id": SESSION, "user_id": USER}
    if answers:
        body["answers"] = answers
    if skip_file:
        body["skip_file"] = skip_file
    label = prompt or (f"answers: {', '.join(answers)}" if answers
                       else f"skip: {skip_file}")
    print(f"--- turn: {label[:100]}")
    post_json("/api/consult", body, out=SSE_PATH)
    render()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, rest = sys.argv[1], sys.argv[2:]

    if cmd == "new":
        curl(["-X", "POST",
              f"{BASE}/api/consult/{SESSION}/reset?user_id={USER}"])
        curl(["-X", "POST", f"{BASE}/api/turn/force-release?user_id={USER}"])
        print(f"session {SESSION} reset")
        send(rest[0] if rest else "Begin the consultation.")

    elif cmd == "say":
        send(rest[0] if rest else "")

    elif cmd == "answer":
        answers = {}
        for arg in rest:
            k, _, v = arg.partition("=")
            if k:
                answers[k.strip()] = v.strip()
        if not answers:
            print("give answers as q_id=value pairs")
            return
        send("", answers=answers)

    elif cmd == "skip":
        send("", skip_file=rest[0] if rest else "")

    elif cmd == "upload":
        for path in rest:
            out = curl(["-X", "POST", f"{BASE}/api/files?user_id={USER}",
                        "-F", f"file=@{path}"])
            print("uploaded:", out)
        send("", prompt="I've uploaded the document you asked for.")

    elif cmd == "state":
        out = curl([f"{BASE}/api/consult/{SESSION}/state?user_id={USER}"])
        try:
            d = json.loads(out)
        except Exception:
            print("NOT JSON:", out[:400])
            return
        print(d["context_block"])
        print("\nterminal:", d["is_terminal"])

    elif cmd == "raw":
        print(open(SSE_PATH).read())

    else:
        print(__doc__)


if __name__ == "__main__":
    main()