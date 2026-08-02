# Decisions

Why this system is built the way it is. Several of these were driven by
things the agent actually did wrong in testing, which is noted where it
applies — those are the more interesting ones.

---

## 1. Harness: a custom loop, not a framework

### What was considered

| Option | What it is | Why not |
|---|---|---|
| **LangGraph** | Graph/state-machine orchestration | Genuinely good state machines, attractive for Agent B. But message construction sits several abstraction layers deep, so "what exactly did we send?" is hard to answer — and that question is the entire brief. |
| **OpenAI Agents SDK** | MIT-licensed, sessions, handoffs, guardrails | Well designed, supports non-OpenAI models via LiteLLM. But it is another vendor's opinionated harness with its own injected scaffolding. Trading one black box for another does not address the complaint. |
| **smolagents** | Minimal; agents write Python you execute | The code-agent paradigm pairs beautifully with Modal Sandboxes. But code-writing agents are hard to constrain for legal safety, and Agent B's "never ask in prose" rule is awkward to enforce. Interesting, wrong fit. |
| **Pydantic AI** | Typed, thin, strong structured output | Closest runner-up. Still owns message assembly, and cache-breakpoint placement would be a fight. What I would pick if prompt bytes were not the thing being optimised. |
| **CrewAI / AutoGen** | Multi-agent orchestration | Large prompt overhead; multi-agent is not a requirement. |
| **LiteLLM** *(adapter, not a harness)* | Unified API across many providers | Would have saved a day. But it lags on provider-specific features — cache control parameters, new streaming event types — and those are precisely the features being graded. |
| **Custom loop** ✅ | ~190 lines | Total control over bytes sent, cache breakpoint placement, halt semantics and token accounting. Trivially debuggable. |

### The reasoning

The stated problem is that a harness the client does not control determines
their prompt overhead. Adopting a different framework reproduces that
problem with a different owner.

`harness/loop.py` is 190 lines. The leverage is in owning them — three of
the five cost drivers in `BENCHMARKS.md` are changes inside that file or
the adapter beneath it, and none would have been reachable through a
framework's public API.

### What the harness turned out to need

Two capabilities emerged from use rather than design:

**A re-resolvable registry.** Phase-based tool gating originally built the
registry once per turn. When a tool changed the phase mid-turn, the model
was told it had new capabilities while the loop still held the old tool set,
and every call failed with "unknown tool" — one turn burned 81,000 input
tokens thrashing. The loop now accepts a callable registry, re-resolved each
iteration. General lesson: **if a tool can change what tools exist, the tool
set cannot be a per-turn constant.**

**Tools that emit events.** `ToolOut.events` lets a tool push events the
loop forwards without inspecting. Citations are emitted this way, so the
harness stays law-agnostic while agents emit domain-specific events.

---

## 2. Model: Claude Sonnet for reasoning, Haiku for mechanical work

Routing is a dict in config:

```python
ROUTES = {
    "loop":    "claude-sonnet-5",
    "draft":   "claude-sonnet-5",
    "extract": "claude-haiku-4-5-20251001",
    "ocr":     "claude-haiku-4-5-20251001",
}
```

Sonnet drives the loop and drafting. Haiku does page relevance extraction,
scanned-page OCR and other mechanical work. Auxiliary calls outnumber loop
calls, so the split matters; traces break cost down per model so it is
measured, not asserted.

**Model-agnosticism is a property of the adapter, not a claim.** The loop
never imports a provider SDK — that is testable with `grep`. Everything
provider-specific lives in `harness/adapters/`: streaming translation,
message construction, tool-definition formatting, cache-control placement,
token counting. Adding a provider is one file implementing five methods.

Alternatives considered: GPT-class models (comparable; choose on measured
caching economics rather than reputation), Gemini (very large context,
strong native PDF handling — a real argument for the OCR path), open-weight
models via Together or Fireworks (viable for the auxiliary calls today, and
the better long-term answer to lock-in).

---

## 3. How document and web content reaches the model

The requirements document explicitly asked for a view on this.

**On-demand tools, outline-first, extraction in the sandbox, hard caps per
call, native multimodal only where there is no text to extract.**

At session start the model sees a manifest — filename, type, size — costing
roughly 15 tokens per file. Nothing else. When it needs content it calls
`read_document`, which defaults to returning an outline (~200 tokens), and
only then reads a named section.

The alternative — pre-extracting uploads into context — is how you reach
135,000 input tokens. A 40-page lease is ~20k tokens; pre-loaded, it is
re-sent on every loop iteration whether the model consulted it or not.

Everything converges on **markdown with heading levels preserved**. That is
load-bearing: headings are what make section reads and section edits
possible. Flatten to plain text and "read the termination clause"
degenerates into "read the whole document".

**OCR splits across the trust boundary.** The sandbox rasterises scanned
pages to images; the API process transcribes them with Haiku. The sandbox
never needs a model, so it never needs a key.

---

## 4. Credential isolation

A hard requirement, and met structurally rather than by policy.

The LLM and search keys are attached to the API function via Modal Secrets.
`Sandbox.create()` takes no `secrets` argument. There is no filter to
bypass and no prompt to argue with — a fully successful jailbreak leaks
nothing, because nothing is there.

Verified from **inside** the sandbox, which is the perspective that matters:
`op_env_check` enumerates the environment and asserts no key-shaped
variables. It runs as part of the worker self-test.

The planned LLM proxy was **designed away rather than built**. Its purpose
was to let sandbox-side tools reach a model without holding a key; moving
OCR to the orchestrator removed the need entirely. A proxy would have been
an attack surface protecting a capability nothing required.

---

## 5. Safety rules driven by observed failures

Five rules in Agent A's system prompt exist because the agent did the wrong
thing in testing, not because the specification listed them.

| Observed | Rule |
|---|---|
| Gave two contradictory Illinois notice periods in one session — "30 days" and "5 days" — depending on conversational framing | Never state a legal rule from memory |
| Computed "payment was due by roughly June 17" from a document date | Do not compute deadlines or apply law to the user's facts |
| Produced `ilga.gov/legislation/ilcs/...` — a well-formed, confidently-presented URL with a wrong path that 404s — for a statute it had never fetched | Cite only URLs fetched in this conversation |
| Drafted an Illinois operating agreement with zero research calls | Research before drafting anything law-dependent |
| Answered "my name is Kartik" with 434 tokens about Chicago's Loop, 71% of that turn's cost | Answer what was asked |

**Prompting alone is not enough, so citations are checked mechanically.**
`web_fetch` returns exact substrings, which are verified against the fetched
text; a quote that does not match is dropped and the citation marked
unverified. After every turn, an audit extracts URLs from the answer and
compares them against what was actually retrieved. A URL in the answer that
is not in the registry is a **ghost citation** and raises a warning.

That check caught the fabricated `ilga.gov` link, which is otherwise
indistinguishable from a real one.

Verified by an eight-case adversarial suite covering model disclosure,
credential probing, prompt injection via uploaded file, unlawful requests,
non-legal requests, directive-language bait, answering from memory, and
deadline computation.

---

## 6. Agent B is configuration, not a second system

Same loop, same adapter, same session and file layers. Four differences:

1. A different system prompt.
2. Five structured-interaction tools.
3. A halt policy — those tools end the turn, and trailing output is
   discarded.
4. A typed state object that gates which tools exist per phase.

**"Never asks in prose" is enforced three ways**, because prompting alone
produced a preamble on one run and none on the next:

- **Tool gating** — `registry.subset(state.allowed_tools())`. In `TERMINAL`
  the subset is empty, so the consultation is over as a matter of what
  exists rather than what the model was told.
- **Halting tools** — the loop breaks and discards anything after the call.
- **Validation in code** — question batches are capped at four and rejected
  if help text is missing or trivially short.

Roughly 200 lines of difference, none of it in the harness.

---

## 7. Session persistence: append-only turn log, no database

`sessions/{id}/turns.jsonl` on the user's volume, one JSON line per turn.
Rebuilding context is replaying the file.

One primitive satisfies four requirements: sandbox recycled → replay;
client reloaded → serve; transcript endpoint → it is the file; debugging →
read the file. No schema, no migrations, no service.

`repair_messages()` guarantees every `tool_use` block has a matching
`tool_result`, synthesising them where a turn was cancelled or crashed.
Without it, one interrupted turn makes a session permanently unusable —
every subsequent request 400s.

---

## 8. Observability: built, not adopted

The specification permits anything that traces a run and shows token and
cost breakdowns.

`tracer.observe(event)` is the entire integration — the loop does not know
the tracer exists. That fell out of defining the event vocabulary before
building components: everything observability needs was already on the wire.

Captures per-model cost rollups (proving the routing), cache-read versus
uncached input separated (they bill differently), a model-time versus
tool-time latency split, and pricing provenance — every cost carries a
`pricing_verified` flag so unverified rates cannot silently reach a
benchmark table.

~250 lines, no dependency. What is given up is a real product: search,
retention, run comparison, eval tooling. Adding a Langfuse or Braintrust
backend is roughly thirty lines in `finish()`, since the trace is already a
serialised dict.

---

## 9. Frontend: one file, no build step

The brief weights this 70/30 and says the frontend is not the focus. A
single self-contained HTML file served by FastAPI — no npm, no
`node_modules`, no `dist/` to go stale, one deploy artefact.

It covers chat with live tool activity and citation confidence,
consultation with natively rendered forms, files, traces, and session
history with reload rehydration.

In production this would integrate with the existing React application.

---

## 10. Things deliberately not built

**RAG, embeddings, a vector database.** Nothing requires it. The corpus is
one user's handful of files, read directly. RAG solves retrieval over a
corpus too large to read; adding it would have cost two days and introduced
chunk-boundary failures for no benefit.

**A general code-execution tool.** The largest prompt-injection surface in
the system, and not required — drafting and file manipulation are covered
by fixed tools.

**Auth, billing, rate limiting.** Out of scope per the requirements.

---

## Appendix: mistakes worth recording

**Rewriting history teaches the model bad patterns.** Compacting answered
question batches by replacing the tool call's arguments with a compact
marker caused the model to imitate that marker as if it were the schema,
calling the tool with `{"_answered": [...]}`. Message history is few-shot
context; anything left in it is an example. The fix removes the
`tool_use`/`tool_result` pair entirely and leaves a plain note — safer, and
saves more (89% on a typical batch).

**Irrelevant context changes behaviour, not just cost.** A volume holding
fifteen generated test files caused the agent to spend iterations reading
uploads unrelated to the question and exhaust its fetch budget before
verifying sources. The same query on an empty volume returned verified
citations quickly. Context relevance matters as much as context size.

**Every repeatable tool needs a budget.** Nothing bounded research
initially. One consultation turn reached 182,000 input tokens by fetching
the same page four times and guessing at section names six times. None of it
was wrong, just unbudgeted — and the model will not impose a budget on
itself.