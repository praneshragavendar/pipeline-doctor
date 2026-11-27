"""LLM access - the "G" (generation) in RAG, and the brain of the investigator agent.

Two providers, both called over plain HTTP (no SDK needed):
  - ollama    : a free local model (default qwen2.5:3b) served by Ollama on this machine or CI runner
  - anthropic : Claude via the Anthropic API (needs ANTHROPIC_API_KEY, paid per use)

Which one is used:
  PIPELINEDOCTOR_LLM=ollama | anthropic   -> that provider
  not set + ANTHROPIC_API_KEY set         -> anthropic
  otherwise                               -> no LLM; the tool still works offline with rules + knowledge base
"""

import json
import os
import urllib.error
import urllib.request
from typing import List, Optional

from pipelinedoctor.models import Incident

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:3b"

# A rule match is more trustworthy than a model guess, so model confidence is capped.
MAX_LLM_CONFIDENCE = 0.8

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
    """Raised when the LLM cannot be used (not configured, network error, bad reply)."""


def provider() -> Optional[str]:
    """Return 'ollama', 'anthropic' or None (no LLM configured)."""
    chosen = os.environ.get("PIPELINEDOCTOR_LLM", "").strip().lower()
    if chosen in ("ollama", "anthropic"):
        return chosen
    if chosen in ("none", "off"):
        return None
    return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else None


def is_configured() -> bool:
    return provider() is not None


def describe() -> str:
    """Human-readable 'provider:model' for reports, e.g. 'ollama:qwen2.5:3b'."""
    name = provider()
    if name == "ollama":
        return f"ollama:{os.environ.get('PIPELINEDOCTOR_OLLAMA_MODEL', DEFAULT_OLLAMA_MODEL)}"
    if name == "anthropic":
        return f"anthropic:{os.environ.get('PIPELINEDOCTOR_MODEL', DEFAULT_MODEL)}"
    return "none"


def _post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise LLMUnavailable(f"LLM request to {url} failed: {exc}") from exc


def _chat_ollama(prompt: str, timeout: int) -> str:
    host = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST).rstrip("/")
    if not host.startswith("http"):
        host = f"http://{host}"
    payload = _post_json(
        f"{host}/api/chat",
        {
            "model": os.environ.get("PIPELINEDOCTOR_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            # Ollama constrains the output to valid JSON - important for small local models.
            "format": "json",
            # temperature 0 = most predictable answers; a larger context window so the
            # instructions are not cut off as the agent's step history grows.
            "options": {"temperature": 0, "num_ctx": 8192},
        },
        {},
        timeout,
    )
    text = (payload.get("message") or {}).get("content")
    if not text:
        raise LLMUnavailable("Ollama returned an empty reply.")
    return text


def _chat_anthropic(prompt: str, timeout: int) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set.")
    payload = _post_json(
        ANTHROPIC_URL,
        {
            "model": os.environ.get("PIPELINEDOCTOR_MODEL", DEFAULT_MODEL),
            "max_tokens": 700,
            "messages": [{"role": "user", "content": prompt}],
        },
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        timeout,
    )
    for block in payload.get("content", []):
        if block.get("type") == "text" and block.get("text"):
            return block["text"]
    raise LLMUnavailable("Anthropic returned an empty reply.")


def chat(prompt: str, timeout: Optional[int] = None) -> str:
    """Send one prompt to the configured LLM and return its text reply."""
    name = provider()
    if name is None:
        raise LLMUnavailable("No LLM configured (set PIPELINEDOCTOR_LLM=ollama or ANTHROPIC_API_KEY).")
    # Local CPU models are slower than a hosted API, so they get a longer default timeout.
    timeout = timeout or int(os.environ.get("PIPELINEDOCTOR_LLM_TIMEOUT", 300 if name == "ollama" else 60))
    return _chat_ollama(prompt, timeout) if name == "ollama" else _chat_anthropic(prompt, timeout)


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMUnavailable("LLM reply did not contain JSON.")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(f"LLM reply was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMUnavailable("LLM reply was JSON but not an object.")
    return data


def normalize_diagnosis(data: dict) -> dict:
    """Validate a model's diagnosis instead of trusting it blindly."""
    hints = data.get("fix_hints") or []
    if not isinstance(hints, list):
        hints = [str(hints)]
    try:
        confidence = float(data.get("confidence", 0.3))
    except (TypeError, ValueError):
        confidence = 0.3
    return {
        "category": str(data.get("category") or "Unknown"),
        "cause": str(data.get("cause") or "").strip() or "The LLM could not determine a cause.",
        "fix_hints": [str(h) for h in hints if str(h).strip()][:5],
        "confidence": max(0.0, min(confidence, MAX_LLM_CONFIDENCE)),
    }


def parse_reply(text: str) -> dict:
    """Parse and validate a one-shot diagnosis reply."""
    return normalize_diagnosis(extract_json(text))


def build_prompt(errors: List[str], incidents: List[Incident]) -> str:
    error_text = "\n".join(errors[:25]) or "(no obvious error lines found)"
    incident_text = "\n".join(
        f"- {i.title}: root cause: {i.root_cause} Fix: {i.fix}" for i in incidents
    ) or "(none)"
    return PROMPT_TEMPLATE.format(errors=error_text, incidents=incident_text)


def diagnose(errors: List[str], incidents: List[Incident], timeout: Optional[int] = None) -> dict:
    """One-shot diagnosis (no tools). Raises LLMUnavailable on any problem."""
    return parse_reply(chat(build_prompt(errors, incidents), timeout=timeout))
