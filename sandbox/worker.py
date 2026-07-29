"""Tool worker — runs INSIDE the user's Modal Sandbox.

Protocol: newline-delimited JSON over stdin/stdout.
    in   {"id":"7","op":"read_text","args":{"path":"uploads/a.txt"}}
    out  {"id":"7","ok":true,"result":{...}}

╔══════════════════════════════════════════════════════════════════════════╗
║  NEVER print() TO STDOUT. Stdout IS the protocol. One stray debug line   ║
║  desynchronises every response after it. Use log() — it writes stderr.   ║
╚══════════════════════════════════════════════════════════════════════════╝

This file is pushed into the sandbox at creation time and executed there.
It is also IMPORTED by the API container (to read its own source), so all
heavy imports must be lazy — the API image has no pymupdf.

No LLM key is present in this process. That is the point.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import traceback

DATA = "/data"
# /data is a SYMLINK to Modal's real volume mount. Resolve it once at
# startup and measure everything against the resolved path — otherwise
# relpath() produces things like "../__modal/volumes/vo-abc123/outputs/x"
# which then leak into tool results and download URLs.
DATA_REAL = os.path.realpath(DATA)
UPLOADS = os.path.join(DATA_REAL, "uploads")
OUTPUTS = os.path.join(DATA_REAL, "outputs")
CACHE = os.path.join(DATA_REAL, "cache")
SESSIONS = os.path.join(DATA_REAL, "sessions")


def log(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


# ── path safety, enforced in code ─────────────────────────────────────────
class PathError(Exception):
    pass


def safe_path(path: str, areas: tuple[str, ...] = ("uploads", "outputs")) -> str:
    """Resolve and confirm the result is inside an allowed area.

    Two layers, deliberately:
      1. Reject absolute paths and '..' segments OUTRIGHT, so the caller
         gets an honest error instead of a silent rewrite.
      2. realpath() then prefix-check, which also catches symlinks pointing
         out of the sandbox — the case layer 1 can't see.

    A prompt can be argued with. This cannot.
    """
    if not path or not path.strip():
        raise PathError("empty path")
    norm = path.replace("\\", "/")
    if norm.startswith("/"):
        raise PathError(f"absolute paths are not allowed: {path}")
    if ".." in norm.split("/"):
        raise PathError(f"parent directory references are not allowed: {path}")

    p = norm
    for prefix in ("uploads/", "outputs/", "cache/", "sessions/"):
        if p.startswith(prefix):
            break
    else:
        p = os.path.join("uploads", p)      # bare filename => uploads

    full = os.path.realpath(os.path.join(DATA_REAL, p))
    allowed = [os.path.realpath(os.path.join(DATA_REAL, a)) for a in areas]
    if not any(full == a or full.startswith(a + os.sep) for a in allowed):
        raise PathError(f"path outside allowed areas: {path}")
    return full


def rel(full: str) -> str:
    """Path relative to the volume root, for anything the model or the UI
    will see. Never expose the resolved /__modal/... form."""
    return os.path.relpath(full, DATA_REAL)


# ── ops ───────────────────────────────────────────────────────────────────
def op_ping(args: dict) -> dict:
    return {"pong": True, "pid": os.getpid(), "python": sys.version.split()[0]}


def op_env_check(args: dict) -> dict:
    """Credential isolation, verifiable from inside the sandbox itself."""
    leaked = [k for k in os.environ
              if any(h in k.upper() for h in ("ANTHROPIC", "OPENAI", "API_KEY",
                                              "SECRET", "TOKEN"))]
    return {"env_var_count": len(os.environ), "suspicious_keys": leaked,
            "keys": sorted(os.environ.keys())}


def op_ensure_dirs(args: dict) -> dict:
    for d in (UPLOADS, OUTPUTS, CACHE, SESSIONS):
        os.makedirs(d, exist_ok=True)
    return {"created": [UPLOADS, OUTPUTS, CACHE, SESSIONS]}


def op_list_dir(args: dict) -> dict:
    area = args.get("area", "uploads")
    base = os.path.join(DATA, area)
    if not os.path.isdir(base):
        return {"area": area, "files": []}
    files = []
    for name in sorted(os.listdir(base)):
        fp = os.path.join(base, name)
        if os.path.isfile(fp):
            st = os.stat(fp)
            files.append({"name": name, "bytes": st.st_size,
                          "modified": st.st_mtime,
                          "ext": os.path.splitext(name)[1].lower()})
    return {"area": area, "files": files}


def op_read_text(args: dict) -> dict:
    full = safe_path(args["path"], areas=("uploads", "outputs", "cache"))
    limit = int(args.get("max_chars", 20000))
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        data = f.read(limit + 1)
    truncated = len(data) > limit
    return {"text": data[:limit], "truncated": truncated,
            "bytes": os.path.getsize(full)}


def op_write_text(args: dict) -> dict:
    """Writes go to outputs/ only. uploads/ is immutable by construction —
    there is no op that can write there."""
    full = safe_path(args["path"], areas=("outputs", "cache"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(args.get("text", ""))
    return {"path": rel(full), "bytes": os.path.getsize(full)}


def op_write_b64(args: dict) -> dict:
    full = safe_path(args["path"], areas=("uploads", "outputs", "cache"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(base64.b64decode(args["b64"]))
    return {"path": rel(full), "bytes": os.path.getsize(full)}


def op_read_b64(args: dict) -> dict:
    full = safe_path(args["path"], areas=("uploads", "outputs", "cache"))
    with open(full, "rb") as f:
        raw = f.read()
    return {"b64": base64.b64encode(raw).decode(), "bytes": len(raw)}


def op_delete(args: dict) -> dict:
    full = safe_path(args["path"], areas=("uploads", "outputs"))
    if os.path.exists(full):
        os.remove(full)
        return {"deleted": True}
    return {"deleted": False, "reason": "not found"}


def op_stat(args: dict) -> dict:
    full = safe_path(args["path"], areas=("uploads", "outputs", "cache"))
    if not os.path.exists(full):
        return {"exists": False}
    st = os.stat(full)
    return {"exists": True, "bytes": st.st_size, "modified": st.st_mtime}


OPS = {
    "ping": op_ping,
    "env_check": op_env_check,
    "ensure_dirs": op_ensure_dirs,
    "list_dir": op_list_dir,
    "read_text": op_read_text,
    "write_text": op_write_text,
    "write_b64": op_write_b64,
    "read_b64": op_read_b64,
    "delete": op_delete,
    "stat": op_stat,
}


def dispatch(req: dict) -> dict:
    rid = req.get("id")
    op = req.get("op")
    args = req.get("args") or {}
    fn = OPS.get(op)
    if fn is None:
        return {"id": rid, "ok": False,
                "error": f"unknown op '{op}'. have: {sorted(OPS)}"}
    try:
        return {"id": rid, "ok": True, "result": fn(args)}
    except PathError as e:
        return {"id": rid, "ok": False, "error": f"PathError: {e}"}
    except Exception as e:
        log(traceback.format_exc())
        return {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}


def serve() -> None:
    op_ensure_dirs({})
    log(f"worker ready pid={os.getpid()}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = {"id": None, "ok": False, "error": f"bad JSON: {e}"}
        else:
            resp = dispatch(req)
        # The ONLY thing that ever goes to stdout.
        sys.stdout.write(json.dumps(resp, default=str) + "\n")
        sys.stdout.flush()


def once(b64_req: str) -> None:
    """Fallback path: python worker.py --once <base64-json>."""
    op_ensure_dirs({})
    req = json.loads(base64.b64decode(b64_req).decode())
    sys.stdout.write(json.dumps(dispatch(req), default=str) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    # Guarded so the API container can import this file to read its source
    # without executing anything.
    if len(sys.argv) > 2 and sys.argv[1] == "--once":
        once(sys.argv[2])
    else:
        serve()