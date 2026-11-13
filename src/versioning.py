"""Prompt version: a short hash of everything that shapes what the LLM is asked.

The system prompt, the business dictionary and the decision schema all change the model's
behavior; any edit to one of them yields a new version, recorded in every trace and response,
so an answer can be tied to the exact prompt that produced it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.agent.models import AgentDecision
from src.agent.prompts import SYSTEM_PROMPT


def prompt_version(dictionary_path: Path) -> str:
    """First 8 hex chars of sha256(system prompt + dictionary bytes + decision JSON schema)."""
    digest = hashlib.sha256()
    digest.update(SYSTEM_PROMPT.encode("utf-8"))
    digest.update(dictionary_path.read_bytes())
    digest.update(json.dumps(AgentDecision.model_json_schema(), sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:8]
