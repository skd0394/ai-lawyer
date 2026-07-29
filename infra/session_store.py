"""Session storage on a per-user Modal Volume.

The API process cannot MOUNT a per-user volume (names are dynamic, mounts
are declared at function-definition time), so we use Modal's direct Volume
API — read_file / batch_upload — which works without mounting.

Layout, inside the user's volume:
    sessions/{session_id}/turns.jsonl   one JSON line per turn
    sessions/{session_id}/state.json    Agent B consultation state (Day 5)
    uploads/                            immutable user uploads
    outputs/                            everything the agent produces
    cache/                              fetched page text, kept OUT of context

KNOWN LIMITATION: appending rewrites the whole file, so writes are O(n) in
session length. Acceptable here (a 50-turn session is <1MB); a production
version would append server-side or use per-turn files.
"""

from __future__ import annotations

import io
import json
import time
from typing import Any

_VOL_CACHE: dict[str, Any] = {}


def user_volume(user_id: str):
    """from_name() is a network lookup, so cache per container."""
    import modal
    if user_id not in _VOL_CACHE:
        _VOL_CACHE[user_id] = modal.Volume.from_name(
            f"ailaw-kartik-user-{user_id}", create_if_missing=True)
    return _VOL_CACHE[user_id]


class SessionStore:
    def __init__(self, user_id: str, session_id: str):
        self.user_id = user_id
        self.session_id = session_id
        self.vol = user_volume(user_id)
        self.turns_path = f"sessions/{session_id}/turns.jsonl"
        self.state_path = f"sessions/{session_id}/state.json"

    # ── raw volume I/O ────────────────────────────────────────────────────
    def _read(self, path: str) -> bytes | None:
        try:
            # reload() picks up writes made by other containers. Skip it and
            # you get stale reads — the classic Modal Volume gotcha.
            self.vol.reload()
        except Exception:
            pass
        try:
            return b"".join(self.vol.read_file(path))
        except Exception:
            return None          # missing file is normal, not an error

    def _write(self, path: str, data: bytes) -> None:
        with self.vol.batch_upload(force=True) as batch:
            batch.put_file(io.BytesIO(data), "/" + path.lstrip("/"))
        try:
            self.vol.commit()
        except Exception:
            pass                 # batch_upload may already have committed

    # ── turns ─────────────────────────────────────────────────────────────
    def read_turns(self) -> list[dict]:
        raw = self._read(self.turns_path)
        if not raw:
            return []
        turns = []
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:
                # A partially-written line from a hard crash. Skip it rather
                # than failing the whole session.
                continue
        return turns

    def append_turn(self, record: dict) -> int:
        existing = self._read(self.turns_path) or b""
        line = json.dumps(record, default=str).encode()
        self._write(self.turns_path, existing + line + b"\n")
        return existing.count(b"\n") + 1

    # ── Agent B state (Day 5) ─────────────────────────────────────────────
    def get_state(self) -> dict:
        raw = self._read(self.state_path)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def set_state(self, state: dict) -> None:
        self._write(self.state_path, json.dumps(state, default=str).encode())

    # ── diagnostics ───────────────────────────────────────────────────────
    def diagnostics(self) -> dict:
        """Report which Volume operations actually work in this Modal
        version, instead of guessing from a failed request."""
        probe = f"sessions/{self.session_id}/.probe"
        result: dict[str, Any] = {"volume": f"ailaw-kartik-user-{self.user_id}"}

        try:
            self._write(probe, b'{"probe":true}\n')
            result["write"] = "ok"
        except Exception as e:
            result["write"] = f"FAILED: {type(e).__name__}: {e}"
            return result

        try:
            back = self._read(probe)
            result["read"] = "ok" if back and b"probe" in back else "MISMATCH"
            result["read_bytes"] = len(back or b"")
        except Exception as e:
            result["read"] = f"FAILED: {type(e).__name__}: {e}"

        try:
            entries = list(self.vol.listdir(f"sessions/{self.session_id}"))
            result["listdir"] = [getattr(e, "path", str(e)) for e in entries]
        except Exception as e:
            result["listdir"] = f"FAILED: {type(e).__name__}: {e}"

        return result


def make_turn_record(*, turn_id: str, prompt: str, messages: list[dict],
                     stop_reason: str | None, usage: dict | None = None,
                     agent: str = "A") -> dict:
    return {
        "turn_id": turn_id,
        "at": time.time(),
        "agent": agent,
        "prompt": prompt,
        "messages": messages,
        "stop_reason": stop_reason,
        "usage": usage or {},
    }