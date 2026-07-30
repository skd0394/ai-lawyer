"""Per-user file storage on a Modal Volume.

Transfer of BYTES goes through the Volume direct API (binary-safe, no
encoding). COMPUTATION over those bytes goes through the sandbox worker.
Different jobs, different constraints, different paths.

Isolation is structural, not a permission check: the volume name embeds
the user id, so there is no code path from user A's request to user B's
volume. Nothing to bypass.

Layout:
    uploads/   immutable — users write here, the agent only reads
    outputs/   everything the agent produces
    cache/     fetched page text, kept OUT of the model's context
    sessions/  turn logs and Agent B state
"""

from __future__ import annotations

import io
import os
import re
import time
from typing import Any

from .session_store import user_volume

MAX_UPLOAD_BYTES = 10 * 1024 * 1024        # spec: "roughly 10 MB cap"

# Deliberately an allowlist. A denylist of dangerous extensions is a game
# you lose; this is the set the agent can actually read.
ALLOWED_EXT = {
    ".pdf", ".docx", ".doc", ".txt", ".md", ".rtf",
    ".csv", ".xlsx", ".xls",
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
}

AREAS = ("uploads", "outputs", "cache")


class FileError(Exception):
    pass


def safe_filename(name: str) -> str:
    """Validate a user-supplied filename.

    Duplicated from worker.safe_path deliberately — uploads never reach the
    worker, so this entry point needs its own check. Sharing the code would
    mean the worker importing API-side modules.
    """
    if not name or not name.strip():
        raise FileError("empty filename")
    base = os.path.basename(name.replace("\\", "/")).strip()
    if base in ("", ".", ".."):
        raise FileError(f"invalid filename: {name!r}")
    if base != name.strip():
        # They sent a path, not a name. Reject rather than silently
        # rewriting — silent rewrites are how you get surprises later.
        raise FileError(f"filename must not contain a path: {name!r}")
    if base.startswith("."):
        raise FileError("filenames may not start with a dot")
    if len(base) > 180:
        raise FileError("filename too long")
    if not re.match(r"^[\w \-.()\[\]+&,']+$", base):
        raise FileError(f"filename contains unsupported characters: {base!r}")
    ext = os.path.splitext(base)[1].lower()
    if ext not in ALLOWED_EXT:
        raise FileError(
            f"unsupported file type '{ext}'. Allowed: "
            f"{', '.join(sorted(ALLOWED_EXT))}")
    return base


def safe_area(area: str) -> str:
    if area not in AREAS:
        raise FileError(f"unknown area '{area}'. Allowed: {AREAS}")
    return area


class FileStore:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.vol = user_volume(user_id)

    # ── write ─────────────────────────────────────────────────────────────
    def save_upload(self, filename: str, data: bytes) -> dict:
        name = safe_filename(filename)
        if len(data) > MAX_UPLOAD_BYTES:
            raise FileError(f"file too large: {len(data)} bytes "
                            f"(limit {MAX_UPLOAD_BYTES})")
        if not data:
            raise FileError("empty file")
        with self.vol.batch_upload(force=True) as batch:
            batch.put_file(io.BytesIO(data), f"/uploads/{name}")
        try:
            self.vol.commit()
        except Exception:
            pass
        return {"name": name, "area": "uploads", "bytes": len(data),
                "ext": os.path.splitext(name)[1].lower()}

    # ── read ──────────────────────────────────────────────────────────────
    def read(self, area: str, filename: str) -> bytes:
        area = safe_area(area)
        name = safe_filename(filename)
        try:
            self.vol.reload()
        except Exception:
            pass
        try:
            return b"".join(self.vol.read_file(f"{area}/{name}"))
        except Exception as e:
            raise FileError(f"not found: {area}/{name} ({type(e).__name__})")

    def list(self, area: str | None = None) -> dict:
        try:
            self.vol.reload()
        except Exception:
            pass
        out: dict[str, list] = {}
        for a in ([safe_area(area)] if area else ("uploads", "outputs")):
            items = []
            try:
                for e in self.vol.listdir(a):
                    path = getattr(e, "path", str(e))
                    nm = os.path.basename(path)
                    if not nm:
                        continue
                    items.append({
                        "name": nm,
                        "area": a,
                        "bytes": getattr(e, "size", None),
                        "ext": os.path.splitext(nm)[1].lower(),
                        "modified": getattr(e, "mtime", None),
                    })
            except Exception:
                items = []          # directory not created yet
            out[a] = sorted(items, key=lambda x: x["name"])
        return out

    def delete(self, area: str, filename: str) -> dict:
        area = safe_area(area)
        name = safe_filename(filename)
        try:
            self.vol.remove_file(f"{area}/{name}")
            try:
                self.vol.commit()
            except Exception:
                pass
            return {"deleted": True, "area": area, "name": name}
        except Exception as e:
            return {"deleted": False, "area": area, "name": name,
                    "reason": f"{type(e).__name__}: {e}"}

    # ── for the model ─────────────────────────────────────────────────────
    def manifest(self) -> str:
        """A COMPACT listing to put in the model's context.

        ~15 tokens per file, not the file contents. This is the heart of
        the on-demand strategy: the model learns what exists and calls
        read_document only for what it actually needs. Pre-loading file
        contents into context is exactly how you get to 135k input tokens.
        """
        listing = self.list()
        lines = []
        for area, label in (("uploads", "uploaded by user"),
                            ("outputs", "generated")):
            for f in listing.get(area, []):
                kb = f"{round((f['bytes'] or 0) / 1024)}KB" if f["bytes"] else "?"
                # Area-qualified name. A bare name reads as ambiguous and
                # the agent guesses the wrong directory; this gives it an
                # unambiguous handle it can pass straight back to a tool.
                lines.append(f"- {area}/{f['name']} "
                             f"({f['ext'].lstrip('.') or 'file'}, {kb}, {label})")
        return "\n".join(lines) if lines else "(no files uploaded yet)"