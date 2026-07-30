"""Agent B consultation state.

The state machine is enforced STRUCTURALLY, not by prompting:

  1. TOOL GATING     which tools exist is a function of phase. In TERMINAL,
                     ask_questions is not in the registry, so the model
                     cannot ask a question even if it wants to.
  2. HALTING TOOLS   the loop breaks on them and discards trailing output.
  3. VALIDATOR       a turn that ends with a prose question and no halting
                     call gets re-prompted once.

Prompting alone cannot deliver "never". Day 2 showed the same prompt
producing a preamble on one run and none on the next; non-determinism means
an instruction is a tendency, not a guarantee.

MERN analogy: an order workflow (pending -> paid -> shipped) where the
router decides which actions are legal for the current status.
"""

from __future__ import annotations

import json
import time
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Phase(str, Enum):
    INTAKE = "INTAKE"          # who are you, what happened
    CLARIFY = "CLARIFY"        # fill the gaps, request documents
    RESEARCH = "RESEARCH"      # look up the law that applies
    SYNTHESIZE = "SYNTHESIZE"  # present findings, prepare to conclude
    TERMINAL = "TERMINAL"      # frozen: handoff or attorney conclusion


# Which tools exist in each phase. This IS the state machine — everything
# else is bookkeeping. A tool absent from the list cannot be called.
TOOLS_BY_PHASE: dict[Phase, list[str]] = {
    Phase.INTAKE: [
        "ask_questions", "request_file", "advance_phase",
    ],
    Phase.CLARIFY: [
        "ask_questions", "request_file", "list_files", "read_document",
        "advance_phase",
    ],
    Phase.RESEARCH: [
        "web_search", "web_fetch", "read_cached_page", "read_document",
        "list_files", "present_findings", "ask_questions", "advance_phase",
    ],
    Phase.SYNTHESIZE: [
        "present_findings", "emit_drafting_handoff",
        "emit_attorney_conclusion", "read_document", "list_files",
    ],
    # Nothing. The consultation is over.
    Phase.TERMINAL: [],
}

HALTING_TOOLS = {
    "ask_questions", "request_file",
    "emit_drafting_handoff", "emit_attorney_conclusion",
}


class AnswerRecord(BaseModel):
    question_id: str
    prompt: str = ""
    answer: Any = None
    at: float = Field(default_factory=time.time)


class FileRequestRecord(BaseModel):
    document_name: str
    reason: str = ""
    status: Literal["pending", "provided", "skipped"] = "pending"
    # When the user skips, the agent recovers by asking questions instead.
    # Tracking this is what stops it re-requesting the same document.
    recovered_via: list[str] = Field(default_factory=list)
    at: float = Field(default_factory=time.time)


class FindingRecord(BaseModel):
    title: str
    summary: str = ""
    detail: str = ""
    severity: Literal["info", "notable", "important", "urgent"] = "info"
    sources: list[dict] = Field(default_factory=list)


class ConsultationState(BaseModel):
    phase: Phase = Phase.INTAKE
    matter_type: str | None = None
    jurisdiction: str | None = None

    collected: dict[str, Any] = Field(default_factory=dict)
    answers: list[AnswerRecord] = Field(default_factory=list)
    # Questions asked but not yet answered. Their presence is why the turn
    # halted; the next user message supplies the answers.
    pending_questions: list[dict] = Field(default_factory=list)

    gaps: list[str] = Field(default_factory=list)
    requested_files: list[FileRequestRecord] = Field(default_factory=list)
    findings: list[FindingRecord] = Field(default_factory=list)

    terminal_kind: Literal["drafting_handoff", "attorney_conclusion",
                           None] = None
    terminal_payload: dict | None = None

    turn_count: int = 0
    questions_asked_count: int = 0

    # ── transitions ───────────────────────────────────────────────────────
    def record_facts(self, jurisdiction: str = "",
                     matter_type: str = "") -> None:
        """The agent has no other way to populate these fields, and
        RESEARCH without a jurisdiction is useless."""
        if jurisdiction and jurisdiction.strip():
            self.jurisdiction = jurisdiction.strip()
        if matter_type and matter_type.strip():
            self.matter_type = matter_type.strip()

    def advance(self, to: Phase) -> bool:
        """Forward-only. Backtracking would let a concluded consultation
        reopen, and 'exactly one of two endings' has to mean exactly one."""
        order = list(Phase)
        if order.index(to) <= order.index(self.phase):
            return False
        if self.phase == Phase.TERMINAL:
            return False
        self.phase = to
        return True

    def allowed_tools(self) -> list[str]:
        return TOOLS_BY_PHASE.get(self.phase, [])

    def record_answers(self, answers: dict[str, Any]) -> int:
        by_id = {q.get("id"): q for q in self.pending_questions}
        n = 0
        for qid, val in (answers or {}).items():
            self.collected[qid] = val
            self.answers.append(AnswerRecord(
                question_id=qid,
                prompt=(by_id.get(qid) or {}).get("prompt", ""),
                answer=val))
            n += 1
        self.pending_questions = []
        return n

    def mark_file(self, name: str, status: str,
                  recovered_via: list[str] | None = None) -> None:
        for r in self.requested_files:
            if r.document_name.lower() == (name or "").lower():
                r.status = status              # type: ignore[assignment]
                if recovered_via:
                    r.recovered_via.extend(recovered_via)
                return
        self.requested_files.append(FileRequestRecord(
            document_name=name, status=status,               # type: ignore
            recovered_via=recovered_via or []))

    def skipped_files(self) -> list[str]:
        return [r.document_name for r in self.requested_files
                if r.status == "skipped"]

    # ── what the model sees ───────────────────────────────────────────────
    def to_context_block(self) -> str:
        """Compact rendering, ~150 tokens — NOT the raw JSON.

        This is what keeps Agent B's context flat across a ten-question
        consultation: the model reads a summary of what has been collected
        instead of re-reading the whole transcript.
        """
        lines = [f"PHASE: {self.phase.value}",
                 f"TURN: {self.turn_count}"]
        if self.matter_type:
            lines.append(f"MATTER: {self.matter_type}")
        lines.append(f"JURISDICTION: {self.jurisdiction or 'NOT YET ESTABLISHED'}")

        if self.collected:
            lines.append("COLLECTED SO FAR:")
            for k, v in list(self.collected.items())[:40]:
                val = str(v)
                lines.append(f"  {k}: {val[:120]}")
        else:
            lines.append("COLLECTED SO FAR: (nothing yet)")

        if self.requested_files:
            lines.append("DOCUMENTS REQUESTED:")
            for r in self.requested_files:
                extra = (f" — recovered via {', '.join(r.recovered_via)}"
                         if r.recovered_via else "")
                lines.append(f"  {r.document_name}: {r.status}{extra}")
            skipped = self.skipped_files()
            if skipped:
                lines.append(f"  NOTE: {', '.join(skipped)} was skipped by "
                             f"the user. Do NOT request it again — collect "
                             f"the same information through questions.")

        if self.pending_questions and self.phase != Phase.TERMINAL:
            # Without this the model cannot tell answered from outstanding,
            # so it REGENERATES the whole batch — ~1,000 output tokens of
            # identical JSON. Listing them costs ~40.
            lines.append("AWAITING ANSWERS (already asked, do NOT re-ask — "
                         "remind the user instead):")
            for q in self.pending_questions[:6]:
                lines.append(f"  {q.get('id')}: {str(q.get('prompt'))[:70]}")

        if self.gaps:
            lines.append("KNOWN GAPS: " + "; ".join(self.gaps[:15]))

        if self.findings:
            lines.append(f"FINDINGS PRESENTED: {len(self.findings)}")

        # NOTE: computed outside the f-string. Nested quotes inside an
        # f-string expression are a SyntaxError before Python 3.12, and
        # these images are pinned to 3.11.
        # A consultation that never ends is both a poor experience and an
        # unbounded cost. Past a threshold, push toward a terminal rather
        # than gathering more.
        if self.questions_asked_count >= 12:
            lines.append(
                f"BUDGET: {self.questions_asked_count} questions asked "
                f"already. Stop gathering. Move to SYNTHESIZE and conclude "
                f"with a handoff or an attorney referral, recording anything "
                f"still unknown in noted_gaps rather than asking for it.")
        elif self.questions_asked_count >= 8:
            lines.append(
                f"BUDGET: {self.questions_asked_count} questions asked. Ask "
                f"at most one more batch, then advance and conclude.")

        tools = ", ".join(self.allowed_tools())
        if not tools:
            tools = "(none — the consultation has concluded)"
        lines.append(f"TOOLS AVAILABLE IN THIS PHASE: {tools}")
        return "\n".join(lines)


# ── pydantic v1 / v2 compatibility ────────────────────────────────────────
# v2 uses model_dump / model_validate / model_copy; v1 uses dict / parse_obj
# / copy. The container turned out to have v1. Rather than pin a version and
# risk breaking whatever pulled it in, feature-detect.

def dump_state(st: "ConsultationState") -> dict:
    """JSON-safe dict. Round-tripping through .json() also normalises the
    Phase enum to a plain string, which v1's .dict() does not."""
    if hasattr(st, "model_dump_json"):
        return json.loads(st.model_dump_json())
    return json.loads(st.json())


def copy_state(st: "ConsultationState") -> "ConsultationState":
    if hasattr(st, "model_copy"):
        return st.model_copy(deep=True)
    return st.copy(deep=True)


def load_state(raw: dict | None) -> ConsultationState:
    """Never raise on a corrupt state file — a bad state should degrade to
    a fresh consultation, not a 500 the user cannot escape."""
    if not raw:
        return ConsultationState()
    try:
        if hasattr(ConsultationState, "model_validate"):
            return ConsultationState.model_validate(raw)
        return ConsultationState.parse_obj(raw)
    except Exception:
        return ConsultationState()