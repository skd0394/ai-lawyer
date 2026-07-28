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

Two consequences. For Day 6, the UI must switch to "Cancelling…" the instant the button is clicked rather than waiting for turn_end, or it looks broken. For your writeup, this is a deliberate tradeoff worth stating: "I check cancellation only at iteration boundaries. Aborting mid-stream would leave a tool_use block without its tool_result and permanently corrupt the session. I accepted up to one iteration of latency to guarantee the session stays replayable." That's the kind of answer that separates a considered design from a lucky one.