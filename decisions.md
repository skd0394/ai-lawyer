Evidence for why the research-and-cite pipeline is mandatory rather than a nice-to-have - you tested the alternative on day one and it gave two different answers to the same question.

Your two calls contradict each other

Call 2: "typically 30 days." Call 3: "5-day notice to pay or quit."

Same jurisdiction, same topic, opposite answers — and the only thing that changed was conversational context nudging the model toward nonpayment rather than a generic termination question. Both replies were confident, well-formatted, and cited nothing. Neither told you which statute it came from or that it was uncertain.

These two answers may correspond to genuinely different provisions of Illinois law — nonpayment and end-of-tenancy termination are typically distinct notice regimes — which makes this worse, not better. A wrong answer that's obviously wrong is safe. A plausible answer that silently applies the wrong provision to a real landlord's real situation is how somebody's eviction gets thrown out.

Legal questions are answered with live web search and page fetching, never from model memory. Legal content should never be hallucinated, so every legal answer cites its sources with URLs."

ROUTES = {
    "loop":    "claude-sonnet-5",              # reasoning, tool orchestration
    "draft":   "claude-sonnet-5",              # document generation
    "extract": "claude-haiku-4-5-20251001",    # page relevance extraction
    "ocr":     "claude-haiku-4-5-20251001",    # scanned page transcription
    "compact": "claude-haiku-4-5-20251001",    # context compaction
    "screen":  "claude-haiku-4-5-20251001",    # injection screening
}


**ls -la ~/ai-lawyer/deploy.sh**


The harness comparison table in your own words — LangGraph, OpenAI Agents SDK, smolagents, Pydantic AI, LiteLLM, custom loop — and why custom.
Model routing: claude-sonnet-5 for the loop, claude-haiku-4-5-20251001 for auxiliary work, verified from the live models endpoint.
The two contradictory eviction answers from T1.3. 30 days vs 5 days, same jurisdiction, neither cited. That's your empirical justification for the research-and-cite pipeline, and it's far more persuasive than quoting the spec back at them.
Today's measurements: cold vs warm sandbox acquire_ms, and the 2,142-vs-822 token observation.


Version	tokens_tool_overhead	Δ
Terse	608	—
Trimmed (live now)	630	+22
Your verbose (never actually deployed)	~800 est.	~+200

749 × 3 iterations  =  2,247 tokens
total billed input  =  2,521 tokens
                       ────────────
static prefix       =     89%



**a small leak worth fixing**
"content_head": "ERROR: cat: /data/uploads/nope.txt: No such file or directory"


limitations.md
**acquire() is not atomic.**
**Stated limitation for LIMITATIONS.md: appending rewrites the whole turns.jsonl, so writes are O(session length). Fine at realistic sizes — a 50-turn session is well under a megabyte — but not how you'd build it for thousands of turns.**

Two consequences. For Day 6, the UI must switch to "Cancelling…" the instant the button is clicked rather than waiting for turn_end, or it looks broken. For your writeup, this is a deliberate tradeoff worth stating: "I check cancellation only at iteration boundaries. Aborting mid-stream would leave a tool_use block without its tool_result and permanently corrupt the session. I accepted up to one iteration of latency to guarantee the session stays replayable." That's the kind of answer that separates a considered design from a lucky one.


model_ms: 7592 vs tool_ms: 151. 98% of wall-clock is the model. Record it, because it should invert meaningfully on Day 4 once web_fetch and its Haiku extraction call enter the picture — and the shift between these two numbers is how you'll explain your latency profile on Day 7.

**are my prices/rates real?**

important:
editing worker.py requires killing the sandbox, not just redeploying. Add it to your notes.

T3.5 — Document extraction

The biggest task of the day, and the one DocDraft explicitly said they want your opinion on:

"The agent can read uploaded documents: text PDFs, scanned PDFs (OCR), DOCX, spreadsheets, images. How the content actually reaches the model (pre-extraction, on-demand tools, native multimodal) is a design decision, and honestly one we're interested in seeing your take on."

Your answer — and be ready to defend it:

"On-demand tools with outline-first reads and hard per-call caps. At session start the model sees a manifest — filename, type, size — costing about 15 tokens per file. Nothing else. When it needs content it calls read_document, which by default returns an outline (~200 tokens), and only then reads a specific section. Extraction runs in the sandbox; native multimodal is used only for images and scanned pages, where there's no text to extract."

On T3.6 — you don't need it

The planned LLM proxy existed so sandbox-side tools could reach a model without holding a key. But the OCR design moved that call to the API process: the sandbox produces pixels, the orchestrator transcribes them. There is now no sandbox-side LLM need at all.

That's a better outcome than building the proxy. Write it up as a decision:

"Rather than proxying LLM access into the sandbox, I arranged the trust boundary so the sandbox never needs a model. It extracts and rasterises; the orchestrator does all inference. The key isn't merely hidden from the sandbox — the sandbox has no reason to want it. A proxy would have been an attack surface protecting a capability nothing needed."

If an evaluator pushes on it, the honest follow-up is: if a future tool genuinely needed sandbox-side inference, you'd mint short-lived HMAC tokens bound to user_id and add a /internal/llm endpoint — but you'd first ask whether the work could move orchestrator-side instead.


"Below ~4,500 chars the tool passes the page through unmodified — extraction costs more than it saves, and I short-circuit rather than pretend otherwise. Above that, reduction scales with page size: 3× at 7k chars, 9–10× at 40k. Legal research fetches are overwhelmingly the large kind — statutory articles, court opinions, CFR parts — so the effective reduction on real workloads sits near the top of that range. Output size is roughly flat at 600–1,000 tokens regardless of input, which is the property that actually matters: page size stops driving context growth."

  1288ch | naive    495 | ours   560 |   0.9x | short-circuit
  1895ch | naive    733 | ours   798 |   0.9x | short-circuit
  7332ch | naive   2337 | ours   740 |   3.2x | extracted
 37173ch | naive  14045 | ours   883 |  15.9x | extracted
 42341ch | naive  15719 | ours  1159 |  13.6x | extracted



 I set an initial tool-definition budget of 1,200 tokens. Measured, seven tools plus the API's fixed scaffolding come to roughly 1,750. I raised the budget rather than cutting further, because I have a measured example of the tradeoff: an ambiguous filename convention caused one turn to burn 34,000 extra input tokens retrying — twenty times the cost of the entire tool-definition block. Descriptions that prevent a wrong tool call pay for themselves many times over. What I did cut was genuine waste: a redundant tool that duplicated an existing mode, and four test-only tools that were being shipped to production turns."