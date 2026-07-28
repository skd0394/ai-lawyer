"""Turn locks over a Modal Dict — cross-container 'is a turn running?'.

MERN analogy: Redis. The API is stateless and horizontally scaled, so the
container that starts a turn is often NOT the one that receives the cancel.
A module-level variable would be invisible to the other container.

Cancellation here is COOPERATIVE. We set a flag; the loop checks it at safe
points and stops itself. You cannot preempt a running async task without
risking a half-written message history.
"""

from __future__ import annotations

import time
from typing import Any

import modal

# If a container dies mid-turn, release() never runs and the user is locked
# out forever. Any lock older than this is treated as abandoned.
STALE_AFTER_S = 15 * 60


class TurnLocks:
    def __init__(self, name: str = "ailaw-kartik-turnlocks"):
        self._name = name
        self._d: Any = None

    @property
    def d(self):
        # Lazy: avoid a network call at import time.
        if self._d is None:
            self._d = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._d

    # ── read ──────────────────────────────────────────────────────────────
    def status(self, user_id: str) -> dict:
        rec = self.d.get(user_id)
        if not rec:
            return {"running": False, "turn_id": None}

        age = time.time() - rec.get("started_at", 0)
        if rec.get("status") == "running" and age > STALE_AFTER_S:
            return {"running": False, "turn_id": rec.get("turn_id"),
                    "stale": True, "age_s": int(age)}

        return {
            "running": rec.get("status") == "running",
            "turn_id": rec.get("turn_id"),
            "session_id": rec.get("session_id"),
            "cancel_requested": rec.get("cancel_requested", False),
            "age_s": int(age),
        }

    # ── write ─────────────────────────────────────────────────────────────
    def acquire(self, user_id: str, turn_id: str, session_id: str = "") -> bool:
        """False if a turn is already running. Caller should 409.

        Not atomic — two simultaneous requests could both win. Modal Dict has
        no compare-and-swap, and for a single-user chat UI the race is not
        worth engineering around. Worth SAYING in the writeup rather than
        pretending it's airtight.
        """
        cur = self.status(user_id)
        if cur["running"]:
            return False
        self.d[user_id] = {
            "status": "running",
            "turn_id": turn_id,
            "session_id": session_id,
            "started_at": time.time(),
            "cancel_requested": False,
        }
        return True

    def release(self, user_id: str) -> None:
        """MUST be called from a `finally`. Skip it and the user is stuck at
        'a turn is already running' until the stale timeout expires."""
        rec = self.d.get(user_id) or {}
        self.d[user_id] = {**rec, "status": "idle",
                           "cancel_requested": False,
                           "ended_at": time.time()}

    def request_cancel(self, user_id: str) -> dict:
        rec = self.d.get(user_id)
        if not rec or rec.get("status") != "running":
            return {"cancelled": False, "reason": "no turn running"}
        self.d[user_id] = {**rec, "cancel_requested": True}
        return {"cancelled": True, "turn_id": rec.get("turn_id")}

    def is_cancelled(self, user_id: str, turn_id: str | None = None) -> bool:
        """Passed into run_turn as cancel_check. One Dict read per loop
        iteration — at most ~12 per turn, negligible."""
        rec = self.d.get(user_id)
        if not rec:
            return False
        if turn_id and rec.get("turn_id") != turn_id:
            return False        # a cancel for a previous turn, ignore
        return bool(rec.get("cancel_requested"))