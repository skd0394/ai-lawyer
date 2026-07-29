"""Client for the in-sandbox worker.

Two transports:
  1. PERSISTENT  one long-lived process; JSON lines over stdin/stdout.
                 Imports happen once. ~10-50ms per call.
  2. ONE-SHOT    `python worker.py --once <b64>` per call. Pays interpreter
                 startup and imports every time (~500ms+), but has no live
                 state to go wrong.

The client tries 1 and falls back to 2. Given how much of Day 3 sits on
this, a degraded path beats a broken one.

NOTE: a ContainerProcess handle is only valid inside the API container that
created it. Another container serving the next turn starts its own worker
in the same sandbox. Harmless — separate processes, shared volume.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from typing import Any

WORKER_DIR = "/tmp/aw"
WORKER_PATH = f"{WORKER_DIR}/worker.py"
EXTRACT_PATH = f"{WORKER_DIR}/extract.py"
DOCX_PATH = f"{WORKER_DIR}/docx_writer.py"

# sandbox_id -> live worker, per API container.
_WORKERS: dict[str, "PersistentWorker"] = {}
_LOCK = threading.Lock()


class WorkerUnavailable(Exception):
    pass


def install_worker(sandbox, source: str, extract_source: str = "",
                   docx_source: str = "") -> None:
    """Push worker.py (and extract.py) into the sandbox.

    base64 avoids every shell quoting problem — the sources contain quotes,
    newlines and backslashes.
    """
    wb = base64.b64encode(source.encode()).decode()
    cmd = (f"mkdir -p {WORKER_DIR} && "
           f"echo '{wb}' | base64 -d > {WORKER_PATH}")
    if extract_source:
        eb = base64.b64encode(extract_source.encode()).decode()
        cmd += f" && echo '{eb}' | base64 -d > {EXTRACT_PATH}"
    if docx_source:
        db = base64.b64encode(docx_source.encode()).decode()
        cmd += f" && echo '{db}' | base64 -d > {DOCX_PATH}"
    cmd += " && echo INSTALLED"

    p = sandbox.exec("bash", "-c", cmd)
    p.wait()
    out = p.stdout.read()
    if "INSTALLED" not in out:
        raise WorkerUnavailable(f"install failed: {out} {p.stderr.read()}")


class PersistentWorker:
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.proc = sandbox.exec("python", "-u", WORKER_PATH)
        self._lines = None
        self._lock = threading.Lock()
        self.dead = False

    def _iter(self):
        if self._lines is None:
            self._lines = iter(self.proc.stdout)
        return self._lines

    def call(self, op: str, args: dict, timeout: int = 60) -> dict:
        if self.dead:
            raise WorkerUnavailable("worker marked dead")
        rid = uuid.uuid4().hex[:8]
        req = json.dumps({"id": rid, "op": op, "args": args}) + "\n"

        # One in-flight request at a time. The protocol is ordered, not
        # multiplexed — no request ids to match out of order.
        with self._lock:
            try:
                self.proc.stdin.write(req.encode())
                self.proc.stdin.drain()
            except Exception as e:
                self.dead = True
                raise WorkerUnavailable(f"write failed: {e}")

            deadline = time.time() + timeout
            for raw in self._iter():
                if time.time() > deadline:
                    self.dead = True
                    raise WorkerUnavailable("timed out waiting for response")
                line = raw.decode() if isinstance(raw, bytes) else raw
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    # Almost always a stray print() to stdout in the worker.
                    self.dead = True
                    raise WorkerUnavailable(
                        f"protocol desync — non-JSON on stdout: {line[:200]!r}")
            self.dead = True
            raise WorkerUnavailable("worker stdout closed")


class SandboxClient:
    def __init__(self, sandbox, worker_source: str,
                 extract_source: str = "", docx_source: str = ""):
        self.sandbox = sandbox
        self.source = worker_source
        self.extract_source = extract_source
        self.docx_source = docx_source
        self.sandbox_id = getattr(sandbox, "object_id", str(id(sandbox)))
        self.transport = "unknown"

    def _persistent(self) -> PersistentWorker:
        with _LOCK:
            w = _WORKERS.get(self.sandbox_id)
            if w is not None and not w.dead:
                return w
            install_worker(self.sandbox, self.source,
                           self.extract_source, self.docx_source)
            w = PersistentWorker(self.sandbox)
            _WORKERS[self.sandbox_id] = w
            return w

    def _oneshot(self, op: str, args: dict, timeout: int = 60) -> dict:
        b64 = base64.b64encode(
            json.dumps({"id": "1", "op": op, "args": args}).encode()).decode()
        install_worker(self.sandbox, self.source, self.extract_source,
                       self.docx_source)
        p = self.sandbox.exec("bash", "-c",
                              f"python {WORKER_PATH} --once {b64}")
        p.wait()
        out = (p.stdout.read() or "").strip()
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return {"ok": False, "error": f"no JSON from worker: "
                                      f"{out[:300]} / {p.stderr.read()[:300]}"}

    def call(self, op: str, args: dict | None = None,
             timeout: int = 60) -> dict:
        args = args or {}
        try:
            resp = self._persistent().call(op, args, timeout=timeout)
            self.transport = "persistent"
            return resp
        except Exception as e:
            # Degrade rather than fail.
            with _LOCK:
                _WORKERS.pop(self.sandbox_id, None)
            self.transport = f"oneshot (persistent failed: {type(e).__name__}: {e})"
            return self._oneshot(op, args, timeout=timeout)

    def ok(self, op: str, args: dict | None = None, timeout: int = 60) -> Any:
        """call() but raises on failure — for internal use where an error
        genuinely should propagate."""
        r = self.call(op, args, timeout=timeout)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "worker call failed"))
        return r["result"]


def worker_source() -> tuple[str, str, str]:
    """Read the sandbox modules' source so they can be pushed in.

    Importing is safe: every heavy import in those modules is lazy, and the
    serve loop is behind `if __name__ == '__main__'`.
    """
    from pathlib import Path
    import sandbox.worker as w
    import sandbox.extract as e
    import sandbox.docx_writer as d
    return (Path(w.__file__).read_text(),
            Path(e.__file__).read_text(),
            Path(d.__file__).read_text())