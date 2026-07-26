"""
AI Lawyer — Modal app.

Day 1 / T1.2 — walking skeleton (health)                    ✅
Day 1 / T1.3 — raw LLM call + usage inspection              ✅
Day 1 / T1.4 — sandbox lifecycle (create / reuse / volume)  ← here
Day 1 / T1.5 — credential isolation test                    ← here

Deploy:  modal deploy modal_app.py

NOTE: Modal's SDK moves fast. If a call below is deprecated, check
https://modal.com/docs/guide/sandbox rather than fighting the error.
"""

import os
import re
import time
import modal

app = modal.App("ailaw-kartik")

# ── Images ────────────────────────────────────────────────────────────────
# TWO images on purpose. The API image stays thin so API cold starts stay
# fast (a number you report on Day 7). The sandbox image carries the heavy
# document libraries the API process never needs.
api_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "fastapi[standard]",
        "anthropic==0.120.0",
        "httpx",
    )
)

sandbox_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "pymupdf",          # PDF text + rasterisation
        "python-docx",      # DOCX read + write
        "openpyxl",         # XLSX
        "trafilatura",      # HTML -> clean markdown
        "httpx",
    )
)

# ⚠️ Attached to the API function ONLY. Never to a Sandbox. See below.
llm_secret = modal.Secret.from_name("anthropic-kartik")

# ── Cross-container state ─────────────────────────────────────────────────
# Redis-equivalent. Needed because the API is stateless and horizontally
# scaled: the container handling turn 2 must discover the sandbox that the
# container handling turn 1 created.
sandbox_registry = modal.Dict.from_name(
    "ailaw-kartik-sandboxes", create_if_missing=True
)

SANDBOX_TIMEOUT_S = 40 * 60      # hard ceiling, matches DocDraft's stated setup
IDLE_TIMEOUT_S = 2 * 60          # Day 3 adds the reaper that enforces this


def _safe_user_id(user_id: str) -> str:
    """Volume names become infrastructure. Never build one from raw input."""
    clean = re.sub(r"[^a-zA-Z0-9-]", "-", user_id)[:40]
    if not clean:
        raise ValueError("invalid user_id")
    return clean


def get_or_create_sandbox(user_id: str):
    """Return (sandbox, was_reused).

    Warm reuse is a product requirement: multi-turn chat must not pay a
    cold start on every message.
    """
    uid = _safe_user_id(user_id)
    rec = sandbox_registry.get(uid)

    if rec:
        try:
            sb = modal.Sandbox.from_id(rec["sandbox_id"])
            # poll() is None => still running. Anything else => it died.
            if sb.poll() is None:
                sandbox_registry[uid] = {**rec, "last_used_at": time.time()}
                return sb, True
        except Exception:
            pass  # stale id; fall through and make a fresh one

    # One Volume per user. This survives sandbox death — it is where
    # uploads, outputs and session state live.
    vol = modal.Volume.from_name(
        f"ailaw-kartik-user-{uid}", create_if_missing=True
    )

    sb = modal.Sandbox.create(
        image=sandbox_image,
        volumes={"/data": vol},
        timeout=SANDBOX_TIMEOUT_S,
        app=app,
        # ═══════════════════════════════════════════════════════════════
        #  NO secrets= ARGUMENT. THIS ABSENCE IS THE HARD REQUIREMENT.
        #  The LLM key lives in the API process and never enters here.
        #  A jailbroken agent that dumps its entire environment leaks
        #  nothing, because there is nothing to leak.
        #  Do not add a secret here "just to debug". Ever.
        # ═══════════════════════════════════════════════════════════════
    )

    sandbox_registry[uid] = {
        "sandbox_id": sb.object_id,
        "created_at": time.time(),
        "last_used_at": time.time(),
    }
    return sb, False


def sh(sb, command: str, timeout: int = 30) -> dict:
    """Run a shell command in the sandbox, return stdout/stderr/exit code."""
    p = sb.exec("bash", "-c", command)
    try:
        p.wait()
    except Exception:
        pass
    return {
        "stdout": p.stdout.read(),
        "stderr": p.stderr.read(),
        "returncode": p.returncode,
    }


@app.function(
    image=api_image,
    secrets=[llm_secret],
    min_containers=0,
    timeout=60 * 15,
)
@modal.asgi_app()
def fastapi_app():
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
    from anthropic import Anthropic

    web_app = FastAPI(title="AI Lawyer API")
    client = Anthropic()

    # ── T1.2 ──────────────────────────────────────────────────────────────
    @web_app.get("/api/health")
    def health():
        key = os.environ.get("ANTHROPIC_API_KEY")
        return {
            "ok": True,
            "service": "ailaw-kartik",
            "llm_key_present": bool(key),
            "llm_key_fingerprint": (key[:7] + "..." + key[-4:]) if key else None,
        }

    # ── T1.3 ──────────────────────────────────────────────────────────────
    @web_app.get("/api/debug/models")
    def list_models():
        return {"models": [
            {"id": m.id, "display_name": getattr(m, "display_name", None)}
            for m in client.models.list(limit=50).data
        ]}

    class LLMDebugRequest(BaseModel):
        prompt: str | None = None
        messages: list[dict] | None = None
        system: str | None = None
        model: str = "claude-haiku-4-5-20251001"
        max_tokens: int = 512

    @web_app.post("/api/debug/llm")
    def debug_llm(req: LLMDebugRequest):
        if not req.prompt and not req.messages:
            raise HTTPException(400, "send `prompt` or `messages`")
        messages = req.messages or [{"role": "user", "content": req.prompt}]
        kwargs = {"model": req.model, "max_tokens": req.max_tokens,
                  "messages": messages}
        if req.system:
            kwargs["system"] = req.system

        t0 = time.perf_counter()
        try:
            r = client.messages.create(**kwargs)
        except Exception as e:
            raise HTTPException(502, f"{type(e).__name__}: {e}")
        latency_ms = int((time.perf_counter() - t0) * 1000)

        return {
            "text": "".join(b.text for b in r.content if b.type == "text"),
            "model": r.model,
            "stop_reason": r.stop_reason,
            "latency_ms": latency_ms,
            "usage": {
                "input_tokens": r.usage.input_tokens,
                "output_tokens": r.usage.output_tokens,
                "cache_read_input_tokens": getattr(
                    r.usage, "cache_read_input_tokens", 0),
                "cache_creation_input_tokens": getattr(
                    r.usage, "cache_creation_input_tokens", 0),
            },
            "messages_sent": len(messages),
        }

    # ── T1.4 ──────────────────────────────────────────────────────────────
    @web_app.post("/api/debug/sandbox")
    def debug_sandbox(user_id: str = "kartik"):
        """Create-or-reuse a sandbox and set up the volume layout.

        Call it twice in a row: the second call must report reused=true
        and be dramatically faster. That gap is warm reuse working.
        """
        t0 = time.perf_counter()
        sb, reused = get_or_create_sandbox(user_id)
        acquire_ms = int((time.perf_counter() - t0) * 1000)

        # The directory conventions the spec relies on:
        #   uploads/  immutable — the agent may read, never write
        #   outputs/  everything the agent produces
        #   sessions/ append-only event logs (Day 3)
        #   cache/    fetched page text, kept OUT of the model's context
        r = sh(sb, "mkdir -p /data/uploads /data/outputs /data/sessions "
                   "/data/cache && ls -la /data && df -h /data | tail -1")

        return {
            "sandbox_id": sb.object_id,
            "reused": reused,
            "acquire_ms": acquire_ms,
            "volume_listing": r["stdout"],
            "stderr": r["stderr"],
        }

    # ── T1.5 — the test that must never fail ──────────────────────────────
    @web_app.get("/api/debug/sandbox/env")
    def sandbox_env(user_id: str = "kartik"):
        """Prove the LLM key is absent from the sandbox.

        This is the demo you will run for the evaluators. They WILL try to
        jailbreak the agent into printing its environment; this shows there
        is nothing there to print.
        """
        sb, _ = get_or_create_sandbox(user_id)
        env = sh(sb, "env; echo '---'; cat /proc/self/environ | tr '\\0' '\\n'")
        blob = env["stdout"]

        leaked = [pat for pat in
                  ("ANTHROPIC_API_KEY", "sk-ant", "OPENAI_API_KEY", "sk-proj")
                  if pat in blob]

        return {
            "key_leaked": bool(leaked),
            "leaked_patterns": leaked,
            "env_var_count": len([l for l in blob.splitlines() if "=" in l]),
            "env_dump": blob,          # read it yourself; trust nothing
        }

    @web_app.delete("/api/debug/sandbox")
    def kill_sandbox(user_id: str = "kartik"):
        """Terminate on demand. Use this when you walk away — it is their
        Modal account paying for idle containers."""
        uid = _safe_user_id(user_id)
        rec = sandbox_registry.get(uid)
        if not rec:
            return {"terminated": False, "reason": "no sandbox registered"}
        try:
            modal.Sandbox.from_id(rec["sandbox_id"]).terminate()
        except Exception as e:
            return {"terminated": False, "error": str(e)}
        del sandbox_registry[uid]
        return {"terminated": True, "sandbox_id": rec["sandbox_id"]}

    return web_app


@app.local_entrypoint()
def main():
    print("Deploy with:  modal deploy modal_app.py")