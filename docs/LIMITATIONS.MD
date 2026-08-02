# Limitations and future work

Built in seven days against a two-week specification. Everything below is a
known gap rather than a discovered one, and most have an identified fix.

---

## Scope taken deliberately

**Seven days, not fourteen.** The specification describes a two-week
project. Where something had to give, it gave in breadth rather than in the
harness, because the harness is what the brief is actually about.

**No RAG, no vector database.** Not required by any requirement. The corpus
is one user's handful of files, read on demand. Adding retrieval
infrastructure would have cost two days and introduced chunk-boundary
failure modes for no benefit.

**No code-execution tool.** The specification mentions the agent executing
code; nothing requires it. Drafting and file manipulation are covered by
fixed tools, and a general `run_python` would be the largest
prompt-injection surface in the system.

**Auth, billing, rate limiting, attorney portal, e-signatures.** Out of
scope per the requirements. Users are identified by an `X-User-Id`-style
parameter.

---

## Known gaps

### Research cost on open-ended consultations

Agent A's drafting path is capped at two searches and two sources, which
brought variance to a 4% spread on the employment query. Agent B's research
phase is capped at six and six per turn, which is looser — a research-heavy
consultation turn can still reach 30–50k input tokens.

*Fix:* within-turn compaction. Once a fetched extract has been used, replace
it with a one-line summary in the history rather than carrying the full
extract to the end of the turn. Roughly a day's work, and it would also help
the demand query, which is the weakest of the three benchmarks at 2.1×.

### The demand query still varies 2.7×

40,665 to 111,589 input tokens across five runs. It performs 3.6 searches
and 5.2 fetches against the other queries' 2/2 — a Texas demand letter has
less obvious primary authority, so the model keeps looking.

*Fix:* lower the per-turn research budget from 6 to 3, at some cost to
research quality on genuinely open questions. Not applied because the
tradeoff should be the client's call.

### Document editing loses bespoke formatting

Editing round-trips DOCX → markdown → DOCX. Structure survives (headings,
tables, lists, placeholders); custom fonts, unusual spacing and embedded
images do not.

Fine for documents the agent drafted itself. For editing a heavily
formatted third-party contract, a production version would manipulate the
OOXML in place.

### An in-flight response is lost if the client disconnects

The user's message is persisted, the turn lock is released, and the trace
is written — so a reload rehydrates the conversation and nothing is
corrupted. But a response still being generated when the connection drops
is not recovered.

Continuing a turn past disconnect means decoupling execution from the
request lifecycle: a durable queue, or a Modal Function per turn writing
events as they occur, with the SSE endpoint reading from that log rather
than driving the loop directly. The event log and replay machinery already
exist; it is the execution boundary that would move.

Verified by `resilience.py --only dropped`.

### Session writes are O(n)

Appending a turn rewrites the whole `turns.jsonl`. Fine at realistic sizes —
a 50-turn session is well under a megabyte — but not how you would build it
for thousands of turns. The Modal Volume API has no append primitive; a
production version would use per-turn files or a real store.

### Turn locks are not atomic

Modal Dict has no compare-and-swap, so two simultaneous requests could in
principle both acquire the lock. For a single-user chat UI the race is
vanishingly unlikely, and the alternative was more machinery than the
problem warrants. Stated rather than hidden.

### Blocking Modal calls inside async handlers

Modal Dict and Volume operations are synchronous and are called from async
request handlers, which logs an `AsyncUsageWarning` and can stall the event
loop under concurrency. Not a problem at demo scale. The fix is
`asyncio.to_thread()` around the lock and volume calls.

### OCR is capped at 8 pages

Scanned PDFs beyond eight pages are transcribed only in part. The omission
is **announced explicitly** in the tool result — the model is told how many
pages were not read and instructed not to treat the document as complete —
because silently summarising half a demand letter is worse than refusing.

### Cold starts are slow and variable

49–118 s measured across the reference queries, against ~288 ms to acquire
a warm sandbox. Sandbox creation dominates. Modal supports pre-warming;
not used here because it costs money continuously on someone else's
account.

### Pricing must be verified before cost figures are quoted

`obs/pricing.py` carries a `PRICING_LAST_VERIFIED` stamp and every trace
carries a `pricing_verified` flag, specifically so unverified rates cannot
silently reach a benchmark table. Token counts and latency are measured
directly and do not depend on it.

### Dependency versions are pinned to what resolved

`fastapi==0.88.0` and `pydantic==1.10.14` — older than current, and what the
Modal image resolved to. Pinned for reproducibility rather than upgraded,
because a rebuild silently pulling pydantic v2 would break every `.dict()`
call. The state layer feature-detects both APIs regardless.

Built on the workspace's legacy image builder, which was left alone because
it is a shared setting.

### Single provider tested at depth

The adapter interface is provider-agnostic and the loop imports no provider
SDK — verifiable with `grep`. But only the Anthropic adapter has been
exercised in anger. A second adapter is a file implementing five methods;
until one has run the full benchmark, "model agnostic" is an architectural
claim rather than a demonstrated one.

### Benchmark sample size

n = 5 warm runs per query against DocDraft's 108. Medians and full ranges
are reported rather than averages alone, and every run's raw metrics are in
`bench_results.json`.

---

## What I would do next, in order

1. **Within-turn context compaction.** The largest remaining cost lever,
   and it closes most of the gap on the demand query.
2. **A second provider adapter run through the full benchmark.** Converts
   model-agnosticism from a design property into a measured one.
3. **Citation re-verification.** Currently a quote is checked against the
   page at fetch time. A second pass at answer time would catch drift
   between research and drafting.
4. **Mid-turn stream resumption.** A dropped connection currently loses the
   stream but not the state; events are already persisted, so replay by
   index is straightforward.
5. **Langfuse or Braintrust backend.** ~30 lines in `Tracer.finish()`.
   Trades a JSONL file for search, retention and run comparison.
6. **Section-aware DOCX editing.** Manipulate OOXML in place rather than
   round-tripping through markdown.