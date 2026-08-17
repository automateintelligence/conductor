"""The Codex CLI adapter. Every Codex-specific string in Conductor belongs here.

Verified against codex-cli 0.147.0 on 2026-08-12; see
``docs/reviews/2026-08-12-codex-host-ground-truth.md``.
"""

from __future__ import annotations


class CodexAdapter:
    id: str = "codex"
