# AI Lawyer — open-source harness

Two legal agents on a custom agent harness, running on Modal.

- **Agent A** — freeform legal assistant. Researches with live web search
  and verified citations, reads uploaded documents, drafts and edits `.docx`
  files.
- **Agent B** — structured consultation. Drives the conversation through
  question forms, document requests and findings panels, ending in either a
  machine-readable drafting handoff or an attorney referral.

Both run on the same loop, adapter, session layer and file layer. Agent B is
roughly 200 lines of configuration on top of Agent A's infrastructure.

## Results at a glance

| | DocDraft | This system |
|---|---:|---:|
| Input tokens (employment) | 135,600 | **26,954** |
| Input tokens (operating) | 135,890 | **33,462** |
| Input tokens (demand) | 144,150 | **68,967** |
| Uncached input | — | **8–18 tokens** |

Full numbers, methodology and cost-driver analysis in
[`BENCHMARKS.md`](BENCHMARKS.md).

## Documents

| | |
|---|---|
| [`DECISIONS.md`](DECISIONS.md) | Harness and model research, architectural decisions, and the failures that drove the safety rules |
| [`BENCHMARKS.md`](BENCHMARKS.md) | Measurements against the three reference queries, and where the cost actually goes |
| [`LIMITATIONS.md`](LIMITATIONS.md) | Known gaps and what I would do next |

---

## Architecture

```
Browser  ──POST /api/chat ─────▶  SSE stream of typed events
         ──POST /api/consult ──▶
         ── REST: files, sessions, traces, turn status, cancel

┌─────────────────────────────────────────────────────────────────┐
│ MODAL APP — API process (FastAPI, @modal.asgi_app)              │
│                                                                 │
│   harness/         provider-agnostic, law-agnostic              │
│     loop.py        the agent loop                               │
│     events.py      the event vocabulary                         │
│     tools.py       registry, dispatch, halting semantics        │
│     session.py     replay, repair, transcript (pure functions)  │
│     adapters/      everything provider-specific lives here      │
│                                                                 │
│   agents/          law-specific, provider-blind                 │
│     common/        search, fetch, citations                     │
│     agent_a/       prompt + tools                               │
│     agent_b/       prompt + tools + consultation state          │
│                                                                 │
│   infra/           Modal-specific: sandboxes, volumes, locks    │
│   obs/             tracing and pricing                          │
│                                                                 │
│   Secrets: LLM + search keys  ◀── live ONLY here                │
│   Dict:    turn locks, sandbox registry                         │
└──────────────────────┬──────────────────────────────────────────┘
                       │ JSONL-RPC over stdin/stdout
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│ MODAL SANDBOX — one named sandbox per user, warm-reused         │
│   worker.py       tool execution, path allowlist                │
│   extract.py      PDF · DOCX · XLSX · CSV · images              │
│   docx_writer.py  markdown → styled .docx, section editing      │
│                                                                 │
│   env: NO KEYS — verified from inside, in the test suite        │
│                                                                 │
│   /data  ◀── per-user Modal Volume                              │
│      uploads/   immutable — no tool can write here              │
│      outputs/   everything the agent produces                   │
│      sessions/  append-only turn log + Agent B state            │
│      cache/     fetched page text, kept OUT of context          │
└─────────────────────────────────────────────────────────────────┘
```

### Two boundaries that carry weight

**`harness/` knows nothing about law; `agents/` knows nothing about
providers.** Both are testable:

```bash
grep -ril "legal\|attorney\|lawyer\|docx" harness/     # empty
grep -rl  "import anthropic" harness/ | grep -v adapters  # empty
```

**The sandbox holds no credentials.** `Sandbox.create()` takes no `secrets`
argument. There is no filter to bypass — a successful jailbreak leaks
nothing because nothing is there. Verified from inside the sandbox in
`/api/debug/worker/selftest`.

### Turn lifecycle

1. Acquire the turn lock (Modal Dict). A second turn gets a 409.
2. Rebuild context by replaying `turns.jsonl` from the user's volume, and
   repair any orphaned tool calls left by an interrupted turn.
3. Run the loop: stream from the adapter, dispatch tools into the sandbox,
   append results, repeat. Cancellation is checked at iteration boundaries.
4. Stream typed events to the client over SSE.
5. In `finally` — persist the turn, release the lock, write the trace.
   Runs on client disconnect too.

---

## Running it

```bash
modal secret create anthropic-<name> ANTHROPIC_API_KEY=...
modal secret create search-<name>    TAVILY_API_KEY=...
modal deploy modal_app.py
```

Open the printed URL. Append `?user=demo` for a clean volume.

### Verification

```bash
curl -X POST "$BASE/api/debug/worker/selftest?user_id=demo"    # isolation, path safety
curl -X POST "$BASE/api/debug/registry/selftest?user_id=demo"  # tool dispatch invariants
curl -X POST "$BASE/api/debug/files/selftest?user_id=demo"     # upload validation, user isolation
curl -X POST "$BASE/api/debug/adversarial"                     # 8-case safety suite
curl -X POST "$BASE/api/debug/budget"                          # prompt + tool token budgets
python3 bench.py --runs 5                                      # the reference queries
```

### Walkthroughs

| Flow | Agent | Prompt |
|---|---|---|
| Freeform drafting | A | *Draft an LLC operating agreement for a two-member consulting business in Chicago, Illinois with 60/40 ownership. The jurisdiction is Illinois state law.* |
| Research with citations | A | *What does it take to evict a commercial tenant in Illinois, and what notices do I need to serve?* |
| Structured consultation | B | *My business partner wants to leave our company and I don't know where to start.* |