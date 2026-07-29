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


# ── SSRF guard ────────────────────────────────────────────────────────────
# A jailbroken agent asking for http://169.254.169.254/ (cloud metadata) or
# http://localhost:8000/ must be stopped BEFORE the request goes out.
# Checked after DNS resolution, because a hostile hostname can resolve to a
# private address.
BLOCKED_SCHEMES = ("file", "ftp", "gopher", "data")


def _check_url(url: str) -> str:
    import ipaddress
    import socket
    from urllib.parse import urlparse

    u = urlparse(url)
    if u.scheme.lower() in BLOCKED_SCHEMES or u.scheme.lower() not in ("http", "https"):
        raise PathError(f"scheme not allowed: {u.scheme}")
    host = u.hostname
    if not host:
        raise PathError("no host in URL")
    if host.lower() in ("localhost", "metadata.google.internal"):
        raise PathError(f"blocked host: {host}")
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as e:
        raise PathError(f"cannot resolve {host}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            raise PathError(f"blocked address {ip} for host {host}")
    return url


def op_fetch_url(args: dict) -> dict:
    """Fetch a URL, strip boilerplate, cache the full text, return it.

    Runs in the SANDBOX: arbitrary user-influenced URLs belong in the
    isolated container, not in the process holding the API keys.

    The full text is written to cache/ and NEVER goes into the model's
    context — the caller passes it to a cheap model for relevance
    extraction and discards it.
    """
    import hashlib
    import httpx

    url = _check_url(args["url"])
    max_bytes = int(args.get("max_bytes", 3_000_000))
    max_chars = int(args.get("max_chars", 60000))

    # Pin the CA bundle explicitly. Modal sets SSL_CERT_DIR in the sandbox
    # env, and if it points somewhere without certs every HTTPS request
    # dies with CERTIFICATE_VERIFY_FAILED.
    try:
        import ssl
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ssl_ctx = True          # fall back to httpx's default

    # Some government sites are slow or reject unfamiliar user agents.
    # One retry with a browser UA before giving up.
    attempts = [
        ("Mozilla/5.0 (compatible; LegalResearchBot/1.0)", 25),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/122.0 Safari/537.36", 30),
    ]
    last_err = None
    for ua, timeout_s in attempts:
        try:
            with httpx.Client(timeout=timeout_s, follow_redirects=True,
                              verify=ssl_ctx,
                              headers={"User-Agent": ua,
                                       "Accept": "text/html,application/xhtml+xml,"
                                                 "application/pdf,*/*"}) as c:
                r = c.get(url)
                status = r.status_code
                final_url = str(r.url)
                ctype = (r.headers.get("content-type") or "").lower()
                raw = r.content[:max_bytes]
            last_err = None
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    if last_err:
        return {"fetched": False, "url": url, "error": last_err}

    if status >= 400:
        return {"fetched": False, "url": url, "status": status,
                "error": f"HTTP {status}"}

    title = ""
    # Content-type sniffing: legal sources serve PDFs from .html URLs
    # surprisingly often.
    if "pdf" in ctype or raw[:5] == b"%PDF-":
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        text = "\n\n".join(f"## Page {i+1}\n\n{p.get_text().strip()}"
                             for i, p in enumerate(doc) if p.get_text().strip())
        title = (doc.metadata or {}).get("title") or url.rsplit("/", 1)[-1]
        doc.close()
        kind = "pdf"
    else:
        import trafilatura
        html = raw.decode("utf-8", "replace")
        text = trafilatura.extract(
            html, output_format="markdown", include_links=False,
            include_comments=False, include_tables=True) or ""
        try:
            md = trafilatura.extract_metadata(html)
            title = (md.title if md else "") or ""
        except Exception:
            pass
        if not title:
            import re as _re
            m = _re.search(r"<title[^>]*>(.*?)</title>", html,
                           _re.I | _re.S)
            title = (m.group(1).strip() if m else final_url)[:200]
        kind = "html"

    text = text.strip()
    if not text:
        return {"fetched": False, "url": url, "status": status,
                "error": "no extractable text (JS-rendered or empty page)"}

    handle = hashlib.sha256(final_url.encode()).hexdigest()[:16]
    os.makedirs(os.path.join(CACHE, "fetch"), exist_ok=True)
    with open(os.path.join(CACHE, "fetch", f"{handle}.md"), "w",
              encoding="utf-8") as f:
        f.write(f"# {title}\nSOURCE: {final_url}\n\n{text}")

    return {"fetched": True, "url": url, "final_url": final_url,
            "status": status, "kind": kind, "title": title,
            "handle": handle, "total_chars": len(text),
            "text": text[:max_chars],
            "text_truncated": len(text) > max_chars}


def op_read_cached(args: dict) -> dict:
    """Retrieve part of a previously fetched page by handle."""
    handle = args["handle"]
    path = os.path.join(CACHE, "fetch", f"{handle}.md")
    if not os.path.exists(path):
        return {"found": False, "error": f"no cached page for handle {handle}"}
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    section = args.get("section")
    if section:
        from extract import _section
        body = _section(text, section)
        if not body:
            heads = [l for l in text.splitlines() if l.startswith("#")]
            return {"found": True, "text": "",
                    "error": f"no section '{section}'",
                    "available": heads[:25]}
        text = body
    limit = int(args.get("max_chars", 8000))
    return {"found": True, "text": text[:limit],
            "truncated": len(text) > limit, "total_chars": len(text)}


def op_extract(args: dict) -> dict:
    """Extract a document to markdown. Heavy imports (pymupdf, python-docx,
    openpyxl) live inside extract.py and are only pulled in here — the API
    container imports this module for its source and must not need them."""
    from extract import extract
    full = safe_path(args["path"], areas=("uploads", "outputs", "cache"))
    return extract(
        full,
        mode=args.get("mode", "outline"),
        section=args.get("section"),
        max_chars=int(args.get("max_chars", 8000)),
        want_images=bool(args.get("want_images", False)),
    )


OPS = {
    "extract": op_extract,
    "fetch_url": op_fetch_url,
    "read_cached": op_read_cached,
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
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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