"""Optional LLM fallback - the "G" (generation) in RAG.

Used only when no rule matches. Sends the error lines plus similar past incidents
to an LLM and asks for a JSON diagnosis. If no API key is set, it is skipped and
the tool still works offline with rules + knowledge base.
"""

import json
import os
import urllib.error
import urllib.request
from typing import List, Optional

from pipelinedoctor.models import Incident

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"

PROMPT_TEMPLATE = """You are a senior DevOps engineer diagnosing a failed CI/CD build.

Error lines from the build log:
{errors}

Similar past incidents and how they were fixed:
{incidents}

Reply with ONLY a JSON object, no other text:
{{"category": "...", "cause": "one or two plain-English sentences",
 "fix_hints": ["step 1", "step 2", "step 3"], "confidence": 0.0}}
Use a confidence between 0 and 1. If the log does not show enough to be sure, say so in
"cause" and keep confidence below 0.5. Do not invent commands that are not standard."""


class LLMUnavailable(Exception):
    """Raised when the LLM cannot be used (no key, network error, bad reply)."""


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def build_prompt(errors: List[str], incidents: List[Incident]) -> str:
    error_text = "\n".join(errors[:25]) or "(no obvious error lines found)"
    incident_text = "\n".join(
        f"- {i.title}: root cause: {i.root_cause} Fix: {i.fix}" for i in incidents
    ) or "(none)"
    return PROMPT_TEMPLATE.format(errors=error_text, incidents=incident_text)


def parse_reply(text: str) -> dict:
    """Pull the JSON object out of the model's reply and validate it."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMUnavailable("LLM reply did not contain JSON.")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(f"LLM reply was not valid JSON: {exc}") from exc

    # Validate the AI output instead of trusting it blindly.
    hints = data.get("fix_hints") or []
    if not isinstance(hints, list):
        hints = [str(hints)]
    try:
        confidence = float(data.get("confidence", 0.3))
    except (TypeError, ValueError):
        confidence = 0.3
    return {
        "category": str(data.get("category", "Unknown")),
        "cause": str(data.get("cause", "")).strip() or "The LLM could not determine a cause.",
        "fix_hints": [str(h) for h in hints][:5],
        # Cap LLM confidence: a rule match is more trustworthy than a model guess.
        "confidence": max(0.0, min(confidence, 0.8)),
    }


def diagnose(errors: List[str], incidents: List[Incident], timeout: int = 30) -> dict:
    """Ask the LLM for a diagnosis. Raises LLMUnavailable on any problem."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set.")
    body = json.dumps(
        {
            "model": os.environ.get("PIPELINEDOCTOR_MODEL", DEFAULT_MODEL),
            "max_tokens": 600,
            "messages": [{"role": "user", "content": build_prompt(errors, incidents)}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        ANTHROPIC_URL,
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LLMUnavailable(f"LLM request failed: {exc}") from exc

    text: Optional[str] = None
    for block in payload.get("content", []):
        if block.get("type") == "text":
            text = block.get("text")
            break
    if not text:
        raise LLMUnavailable("LLM returned an empty reply.")
    return parse_reply(text)
