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