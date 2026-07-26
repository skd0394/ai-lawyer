"""
AI Lawyer — Modal app.

Day 1 / T1.2 — walking skeleton (health endpoint)          ✅
Day 1 / T1.3 — raw LLM call + usage inspection             ← you are here

Deploy:
    modal deploy modal_app.py
"""

import os
import time
import modal

app = modal.App("ailaw-kartik")

api_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "fastapi[standard]",
        "anthropic==0.121.0",
        "httpx",
    )
)

# ⚠️ Attached to THIS function only. Day 3's Sandbox.create() gets no secrets=.
#    That omission is the credential-isolation guarantee.
llm_secret = modal.Secret.from_name("anthropic-kartik")


@app.function(
    image=api_image,
    secrets=[llm_secret],
    min_containers=0,
    timeout=60 * 15,
)
@modal.asgi_app()
def fastapi_app():
    # Imports live IN here: these packages exist in the remote image,
    # not on your laptop.
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
    from anthropic import Anthropic

    web_app = FastAPI(title="AI Lawyer API")

    # The client reads ANTHROPIC_API_KEY from the environment, which Modal
    # injected from the Secret. Constructed once per container, not per
    # request — connection reuse matters when you're doing 12 calls a turn.
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
        """Get the REAL current model IDs from the API.

        Don't trust model IDs from blog posts, tutorials, or me — they go
        stale. This is the source of truth for your config on Day 2.
        """
        return {"models": [
            {"id": m.id, "display_name": getattr(m, "display_name", None)}
            for m in client.models.list(limit=50).data
        ]}

    class LLMDebugRequest(BaseModel):
        # Send EITHER a single prompt...
        prompt: str | None = None
        # ...OR a full message array, so you can watch input_tokens grow
        # as history accumulates. That growth is the whole project.
        messages: list[dict] | None = None
        system: str | None = None
        model: str = "claude-haiku-4-5-20251001"   # verify via /api/debug/models
        max_tokens: int = 512

    @web_app.post("/api/debug/llm")
    def debug_llm(req: LLMDebugRequest):
        if not req.prompt and not req.messages:
            raise HTTPException(400, "send `prompt` or `messages`")

        messages = req.messages or [{"role": "user", "content": req.prompt}]

        kwargs = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "messages": messages,
        }
        if req.system:
            kwargs["system"] = req.system

        t0 = time.perf_counter()
        try:
            r = client.messages.create(**kwargs)
        except Exception as e:
            # Day 2 turns this into retry-with-backoff + a clean `error`
            # event. For now, just surface it honestly rather than hanging.
            raise HTTPException(502, f"{type(e).__name__}: {e}")
        latency_ms = int((time.perf_counter() - t0) * 1000)

        text = "".join(b.text for b in r.content if b.type == "text")

        return {
            "text": text,
            "model": r.model,
            "stop_reason": r.stop_reason,
            "latency_ms": latency_ms,
            # ⭐ STARE AT THIS OBJECT. It is the scoreboard for the
            #    entire project. Note that cache_* fields are 0 right
            #    now — you'll light those up on Day 2 (T2.6).
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

    return web_app


@app.local_entrypoint()
def main():
    print("Deploy with:  modal deploy modal_app.py")