"""Prompt version: a short hash of everything that shapes what the LLM is asked.

The source files that build the messages (system prompt, conversation note, history window,
message template, the privacy note on masked tokens), the business dictionary and the decision
schema all change the model's behavior; any edit to one of them yields a new version, recorded
in every trace and response, so an answer can be tied to the exact prompt that produced it.

Hashing whole source files is deliberately coarse: a comment edit also bumps the version, which
costs a re-run of the evaluation, never a silently mislabeled one. Line endings are normalized first,
so a Windows clone (``core.autocrlf=true``) reports the same version as the published evaluation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from src import privacy
from src.agent import prompts
from src.agent.models import AgentDecision

PROMPT_SOURCES: tuple[Path, ...] = (Path(prompts.__file__), Path(privacy.__file__))


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def prompt_version(dictionary_path: Path, sources: Sequence[Path] = PROMPT_SOURCES) -> str:
    """First 8 hex chars of sha256(prompt source files + dictionary bytes + decision JSON schema)."""
    digest = hashlib.sha256()
    for path in (*sources, dictionary_path):
        digest.update(_lf(path.read_bytes()))
    digest.update(json.dumps(AgentDecision.model_json_schema(), sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:8]
