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
    # Ships your local harness/ package into the image. Must come AFTER
    # pip_install — Modal requires local layers last. Without this you get
    # ModuleNotFoundError: no module named 'harness' at runtime, because
    # Modal only auto-mounts the entrypoint file, not your packages.
    .add_local_python_source("harness")
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

    # ── T1.6 — the first agent loop ───────────────────────────────────────
    # An LLM alone can only talk. An LLM in a loop with tools can act.
    # Note the split: this loop runs HERE, in the API process, which holds
    # the key. The tools run in the SANDBOX, which does not. That is the
    # architecture for the rest of the project.

    AGENT_TOOLS = [
        {
            "name": "list_files",
            # Descriptions are documentation for the model. They are also
            # re-sent on EVERY iteration, so every word costs you N times.
            # Terse and precise, always.
            "description": "List the user's uploaded files.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "read_file",
            # TRIMMED version. Every behavioural constraint kept, the
            # redundant restatements removed. Safety is enforced in _read()
            # by a code check — this prose only prevents wasted iterations.
            "description": (
                "Read one uploaded file. Call list_files first \u2014 the name must be "
                "an exact filename from that list, with no directory path."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "e.g. notes.txt"}
                },
                "required": ["name"],
            },
        },
    ]

    def run_agent_tool(sb, name: str, args: dict) -> str:
        """Dispatch. NEVER raises — a tool error is information the model
        can act on. An exception here would kill the whole turn."""
        import shlex
        try:
            if name == "list_files":
                return sh(sb, "ls -1 /data/uploads")["stdout"].strip() or "(no files)"
            if name == "read_file":
                fn = args.get("name", "")
                # Path safety enforced in CODE, not in the prompt. A prompt
                # can be argued with; a string check cannot. Day 3 hardens
                # this into a proper realpath allowlist.
                if not fn or "/" in fn or ".." in fn:
                    return "ERROR: filename only, no paths"
                out = sh(sb, f"cat /data/uploads/{shlex.quote(fn)}")
                return out["stdout"] or f"ERROR: {out['stderr'].strip()}"
            return f"ERROR: no tool named {name}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    class AgentRequest(BaseModel):
        prompt: str = "What files do I have, and what is in them?"
        user_id: str = "kartik"
        model: str = "claude-haiku-4-5-20251001"
        max_iterations: int = 6

    @web_app.post("/api/debug/agent")
    def debug_agent(req: AgentRequest):
        sb, _ = get_or_create_sandbox(req.user_id)

        # Seed a sample file so there is something to read.
        sh(sb, "mkdir -p /data/uploads && [ -f /data/uploads/notice.txt ] || "
               "cat > /data/uploads/notice.txt << 'EOF'\n"
               "FIVE DAY NOTICE\n\n"
               "To: Tenant, 400 W Madison St, Chicago IL\n"
               "You are hereby notified that rent of $4,200 is past due.\n"
               "Payment must be made within five days of service of this notice.\n"
               "EOF")

        messages = [{"role": "user", "content": req.prompt}]
        iterations, final_text = [], ""
        t0 = time.perf_counter()

        for n in range(1, req.max_iterations + 1):
            try:
                r = client.messages.create(
                    model=req.model,
                    max_tokens=1024,
                    tools=AGENT_TOOLS,      # re-sent every single iteration
                    messages=messages,
                )
            except Exception as e:
                raise HTTPException(502, f"iteration {n}: {type(e).__name__}: {e}")

            calls = [b for b in r.content if b.type == "tool_use"]
            iterations.append({
                "n": n,
                "input_tokens": r.usage.input_tokens,
                "output_tokens": r.usage.output_tokens,
                "stop_reason": r.stop_reason,
                "tool_calls": [c.name for c in calls],
            })

            messages.append({"role": "assistant", "content": r.content})

            if not calls:
                final_text = "".join(b.text for b in r.content if b.type == "text")
                break

            results = []
            for c in calls:
                out = run_agent_tool(sb, c.name, c.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": c.id,
                    # Cap tool output. Uncapped results are how a 200-page
                    # PDF ends up costing you 6x its size in an agent loop.
                    "content": out[:4000],
                })
            messages.append({"role": "user", "content": results})
        else:
            final_text = "(hit max_iterations without finishing)"

        billed_in = sum(i["input_tokens"] for i in iterations)
        return {
            "final_text": final_text,
            "iterations": iterations,
            "totals": {
                # ⭐ THE NUMBER. Compare it to the LAST iteration's
                #    input_tokens. The gap is what re-sending history costs.
                "billed_input_tokens": billed_in,
                "billed_output_tokens": sum(i["output_tokens"] for i in iterations),
                "final_context_tokens": iterations[-1]["input_tokens"],
                "iterations_used": len(iterations),
                "latency_ms": int((time.perf_counter() - t0) * 1000),
            },
        }

    # ── T2.1 + T2.2 — event vocabulary and model adapter ──────────────────
    class AdapterTestRequest(BaseModel):
        prompt: str = "List my files, then read whichever one you find."
        model: str = "claude-haiku-4-5-20251001"
        with_tools: bool = True
        system: str | None = None

    @web_app.post("/api/debug/adapter")
    async def debug_adapter(req: AdapterTestRequest):
        """Exercise the adapter in isolation — no loop yet.

        Proves: provider stream -> OUR event vocabulary, tool arguments
        accumulate correctly from partial JSON, usage is captured, and
        message construction round-trips.
        """
        from harness.adapters.anthropic_adapter import AnthropicAdapter
        from harness.adapters.base import ToolResult
        from harness.events import (
            StreamEnd, TextDelta, ToolUseEnd, ToolUseStart, Usage,
        )

        adapter = AnthropicAdapter()
        messages = [{"role": "user", "content": req.prompt}]
        tools = AGENT_TOOLS if req.with_tools else None

        # Budget check BEFORE sending — this is the mechanism that will
        # enforce <1500 system / <1200 tool-definition limits.
        pre_count = await adapter.count_tokens(
            model=req.model, messages=messages,
            system=req.system, tools=tools,
        )
        # Count again with NO tools, so we can isolate what the tool
        # definitions actually cost from what the prompt costs.
        bare_count = await adapter.count_tokens(
            model=req.model, messages=messages,
            system=req.system, tools=None,
        )

        counts: dict[str, int] = {}
        text = ""
        tool_calls: list = []
        usage = None
        stop_reason = None

        async for ev in adapter.stream(
            model=req.model, messages=messages,
            system=req.system, tools=tools, max_tokens=1024,
        ):
            counts[ev.type] = counts.get(ev.type, 0) + 1
            if isinstance(ev, TextDelta):
                text += ev.text
            elif isinstance(ev, ToolUseEnd):
                tool_calls.append(ev)
            elif isinstance(ev, Usage):
                usage = ev
            elif isinstance(ev, StreamEnd):
                stop_reason = ev.stop_reason

        # Round-trip the message builders: everything provider-shaped
        # lives in the adapter, so the loop never sees Anthropic's format.
        assistant_msg = adapter.assistant_message(text, tool_calls)
        result_msg = adapter.tool_result_message([
            ToolResult(call_id=c.id, name=c.name, content="(stub)", ok=True)
            for c in tool_calls
        ]) if tool_calls else None

        return {
            "event_counts": counts,
            "text": text,
            "tool_calls": [{"id": c.id, "name": c.name, "args": c.args}
                           for c in tool_calls],
            "stop_reason": stop_reason,
            "usage": usage.to_dict() if usage else None,
            "pre_send_token_count": pre_count,
            # ── Provenance. Never guess whether a redeploy landed again. ──
            "tokens_prompt_only": bare_count,
            "tokens_tool_overhead": pre_count - bare_count,
            # Echo back the EXACT definitions being sent to the model.
            # If this doesn't match your local file, you're serving stale code.
            "tools_sent": [
                {"name": t["name"],
                 "description": t["description"],
                 "description_chars": len(t["description"])}
                for t in (tools or [])
            ],
            "constructed_assistant_message": assistant_msg,
            "constructed_tool_result_message": result_msg,
            # If this is >1 for a normal reply, streaming is working:
            # you received the answer in fragments, not one blob.
            "text_delta_count": counts.get("text_delta", 0),
        }

    # ── T2.3 + T2.4 — registry + real loop ────────────────────────────────
    def build_debug_registry():
        """Toy registry to exercise the loop. Day 4 replaces this with
        agents/agent_a/registry.py — the loop itself won't change."""
        from harness.tools import ToolRegistry, ToolOut

        reg = ToolRegistry()

        @reg.tool(name="list_files",
                  description="List the user's uploaded files.",
                  schema={"type": "object", "properties": {}, "required": []})
        def _list(args, ctx):
            out = sh(ctx["sandbox"], "ls -1 /data/uploads")
            names = out["stdout"].strip()
            return ToolOut(content=names or "(no files)",
                           summary=f"{len(names.splitlines())} file(s)")

        @reg.tool(name="read_file",
                  description="Read one uploaded file. Call list_files first.",
                  schema={"type": "object",
                          "properties": {"name": {"type": "string"}},
                          "required": ["name"]},
                  max_result_chars=4000)
        def _read(args, ctx):
            import shlex
            fn = args.get("name", "")
            if not fn or "/" in fn or ".." in fn:
                return ToolOut(ok=False, summary="bad filename",
                               content="ERROR: filename only, no paths.")
            out = sh(ctx["sandbox"], f"cat /data/uploads/{shlex.quote(fn)}")
            if out["returncode"] != 0:
                return ToolOut(ok=False, summary="not found",
                               content=f"ERROR: {out['stderr'].strip()}")
            return ToolOut(content=out["stdout"], summary=f"read {fn}")

        # A tool that always fails — proves the loop survives tool errors
        # and the model recovers instead of the turn dying.
        @reg.tool(name="broken_tool",
                  description="Deliberately broken. For testing only.",
                  schema={"type": "object", "properties": {}, "required": []})
        def _broken(args, ctx):
            raise RuntimeError("this tool is intentionally broken")

        return reg

    class LoopRequest(BaseModel):
        prompt: str = "What files do I have, and what do they say?"
        user_id: str = "kartik"
        model: str = "claude-sonnet-5"
        max_iterations: int = 8
        cache: bool = False

    @web_app.post("/api/debug/loop")
    async def debug_loop(req: LoopRequest):
        """The real harness, running end to end. Returns the full event
        stream as JSON so you can inspect it before T2.8 adds SSE."""
        from harness.adapters.anthropic_adapter import AnthropicAdapter
        from harness.loop import run_turn

        sb, reused = get_or_create_sandbox(req.user_id)
        sh(sb, "mkdir -p /data/uploads && [ -f /data/uploads/notice.txt ] || "
               "printf 'FIVE DAY NOTICE\\n\\nRent of $4,200 is past due for "
               "400 W Madison St, Chicago IL.\\nPay within five days.\\n' "
               "> /data/uploads/notice.txt")

        messages: list[dict] = [{"role": "user", "content": req.prompt}]
        collected: list[dict] = []
        text = ""

        async for ev in run_turn(
            adapter=AnthropicAdapter(),
            model=req.model,
            messages=messages,          # mutated in place; caller persists
            registry=build_debug_registry(),
            system="You are a helpful assistant with access to the user's files.",
            ctx={"sandbox": sb, "user_id": req.user_id},
            max_iterations=req.max_iterations,
            cache=req.cache,
            session_id="debug",
        ):
            d = ev.to_dict()
            if ev.type == "text_delta":
                text += d["text"]
                continue          # collapse deltas for readable output
            collected.append(d)

        return {
            "sandbox_reused": reused,
            "final_text": text,
            "events": collected,
            # messages now holds the full turn — this is what Day 3 persists
            # to events.jsonl so the session survives sandbox recycling.
            "message_count": len(messages),
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