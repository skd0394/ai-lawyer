# Benchmarks

Measured against the three reference queries from the requirements document,
using the prompts verbatim.

## Method

- Dedicated empty user volume. A cluttered file manifest was measured to
  change the agent's behaviour, not just its token count, so generated
  files are cleared before every run.
- Fresh session per run; history never accumulates across runs.
- One discarded warm-up run before measuring.
- Cold start measured separately by terminating the sandbox first.
- **n = 5 warm runs per query.** DocDraft's figures are averages over 108
  runs. Medians are reported here because the sample is small, and the
  range table below matters as much as the median.
- All 15 runs produced a document. Runs that fail to produce one are
  excluded from the statistics and reported separately — the benchmark is
  document generation, and a non-draft is a failure to measure rather than
  a cheap success.

> ⚠️ **Verify pricing before quoting the cost column.** Rates live in
> `obs/pricing.py` with a `PRICING_LAST_VERIFIED` stamp; traces carry a
> `pricing_verified` flag. Token and latency figures are measured directly
> and do not depend on it.

## Results

| Query | Metric | DocDraft | This system | Δ |
|---|---|---:|---:|---:|
| employment | input tokens | 135,600 | 26,954 | **5.0× lower** |
| employment | output tokens | 13,830 | 6,051 | 2.3× lower |
| employment | total tokens | 149,400 | 33,024 | **4.5× lower** |
| employment | cost | $0.358 | $0.130 | 2.8× lower |
| employment | latency (median) | 236 s | 82 s | 2.9× lower |
| demand | input tokens | 144,150 | 68,967 | **2.1× lower** |
| demand | output tokens | 6,740 | 5,674 | 1.2× lower |
| demand | total tokens | 150,900 | 74,329 | 2.0× lower |
| demand | cost | $0.205 | $0.146 | 1.4× lower |
| demand | latency (median) | 133 s | 69 s | 1.9× lower |
| operating | input tokens | 135,890 | 33,462 | **4.1× lower** |
| operating | output tokens | 15,050 | 8,483 | 1.8× lower |
| operating | total tokens | 150,900 | 42,351 | 3.6× lower |
| operating | cost | $0.361 | $0.173 | 2.1× lower |
| operating | latency (median) | 275 s | 97 s | 2.8× lower |

### Composition

| Query | uncached in | cache read | cache write | model calls | searches | fetches | words | placeholders | cold start |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| employment | 8 | 17,963 | 8,987 | 4.0 | 2.4 | 1.6 | 1,820 | 18.6 | 94 s |
| demand | 18 | 66,923 | 10,549 | 9.2 | 3.6 | 5.2 | 696 | 20.6 | 49 s |
| operating | 8 | 21,638 | 12,222 | 4.0 | 2.0 | 2.0 | 2,790 | 20.4 | 118 s |

### Variance

| Query | n | input min–max | latency min–max | documents |
|---|---:|---|---|---:|
| employment | 5 | 26,410 – 27,554 | 70 – 92 s | 5/5 |
| demand | 5 | 40,665 – 111,589 | 57 – 87 s | 5/5 |
| operating | 5 | 31,542 – 36,391 | 92 – 109 s | 5/5 |

## The biggest cost drivers

### 1. Fetched page content re-sent across loop iterations

The dominant driver. An agent loop re-sends the entire message history on
every iteration, so a page placed in context at step 3 is billed again at
steps 4, 5, 6 and 7.

Measured on real statutory pages, counting tokens rather than estimating
(legal text tokenises at ~2.6 chars/token, not the usual ~4):

| Page | chars | tokens if passed through | tokens as returned | reduction |
|---|---:|---:|---:|---:|
| 29 CFR 541.100 | 1,288 | 495 | 560 | 0.9× |
| 735 ILCS 5/9-209 | 1,895 | 733 | 798 | 0.9× |
| Cornell — eviction | 7,332 | 2,337 | 740 | 3.2× |
| 11 USC 362 | 37,173 | 14,045 | 883 | **15.9×** |
| 29 USC 206 | 42,341 | 15,719 | 1,159 | **13.6×** |

`web_fetch` runs in the sandbox, writes the full cleaned text to the user's
volume, and returns only the passages relevant to a stated `purpose` —
extracted by a cheap model — plus an outline and a handle for retrieving
more. Output size is roughly flat (560–1,159 tokens) regardless of whether
the page is 1k or 42k characters. That flatness is the property that
matters: page size stops driving context growth.

Below ~4,500 characters the tool passes the page through unmodified.
Extraction costs more than it saves on short pages, and short-circuiting is
more honest than reporting a favourable average across mixed sizes.

### 2. Prompt caching across loop iterations

Within one drafting turn the loop makes 4–9 model calls, and call N's
prefix is call N−1's messages plus one tool result. That prefix is stable
and therefore cacheable — including the accumulated research.

Three cache breakpoints: after the system prompt, after the tool
definitions, and a **rolling breakpoint on the last message** each
iteration.

The rolling breakpoint was added late, and the before/after is stark:

| | uncached input | cache read | share cached |
|---|---:|---:|---:|
| static breakpoints only | 102,230 | 26,917 | 21% |
| with rolling breakpoint | 8 | 17,963 | >99% |

This is the mechanism behind the requirements document's own observation
that its reported cost sits below what raw token counts imply.

### 3. Prompt and tool-definition overhead

Re-sent on every iteration, so a 200-token overrun costs 1,600 tokens on an
eight-iteration turn. Measured with the provider's token-counting endpoint
rather than estimated:

| Component | tokens |
|---|---:|
| API scaffolding (present whenever tools are enabled) | 354 |
| 7 tool definitions | 1,138 |
| System prompt | ~1,020 |
| **Static prefix** | **~2,500** |

Two tools were removed after measurement: one duplicated an existing tool's
mode, and four test-only tools had been shipping to production turns.

The initial budget of 1,200 tokens for tool definitions was raised to 1,800
deliberately. A single wrong tool call costs a whole extra iteration — an
ambiguous filename convention was measured costing one turn **34,000 extra
input tokens**, roughly twenty times the entire tool-definition block.
Descriptions that prevent wrong calls pay for themselves many times over.

### 4. Model routing

The loop and drafting run on Sonnet. Page relevance extraction, scanned-page
OCR and other mechanical work run on Haiku. Traces break cost down per
model, so the split is measured rather than asserted.

### 5. Bounded research

Unbounded, the same drafting prompt varied from **22,887 to 216,835** input
tokens depending on how much the model chose to investigate — and in some
runs it researched instead of drafting at all.

Drafting requests are now capped at two searches and two sources, with an
explicit requirement to produce the document. Employment variance fell to a
4% spread across five runs.

A per-turn budget of six searches and six fetches is also enforced in code,
with repeat URLs served from an in-turn cache. Before that, one consultation
turn reached 182,000 input tokens by fetching the same statute page four
times and guessing at section names that did not exist.

## Latency

Median 69–97 s against the reference's 133–275 s. Traces split wall-clock
into model time and tool time, so regressions are attributable.

Cold start (sandbox creation, from terminated) is 49–118 s and is not
representative of steady state; a warm sandbox acquires in ~288 ms against
~853 ms cold, and a warm in-sandbox tool call costs ~16 ms.

Note the honest tradeoff: extraction adds a model round trip per fetched
page. That cost is real, and it is why latency improves less than input
tokens do.

## Where the remaining gap is

**Demand** is the weakest of the three at 2.1× and shows a 2.7× spread. It
performs 3.6 searches and 5.2 fetches against the others' 2/2 — a Texas
demand letter has less obvious primary authority, so the model keeps
looking. Tightening the per-turn research budget from 6 to 3 would likely
close most of it, at some cost to research quality on genuinely open
questions.

**Output tokens** improve least, and that is correct. On a drafting task
the output *is* the product. A large reduction there would mean a worse
document.