"""Investigator agent: lets the LLM decide how to investigate an unknown failure.

This is the agentic part (a ReAct loop: reason -> act -> observe -> repeat).
Instead of a fixed script, the model picks one tool per step, sees the result,
and decides what to do next until it is ready to give a diagnosis.

Tools are deliberately read-only: the agent can look at the log and the knowledge
base, but it cannot change anything. Actions that change things (like retrying a
build) are decided by deterministic rules, never by a model guess.
"""

import json
import re
from typing import Any, Dict, List, Tuple

from pipelinedoctor import llm
from pipelinedoctor.knowledge import KnowledgeBase

MAX_TOOL_CALLS = 6
MAX_OBSERVATION_CHARS = 1500
MAX_READ_LINES = 80
CONTEXT_LINES = 8  # lines shown before/after the first error, so the agent starts with context
TOOLS = ("search_log", "read_lines", "search_incidents", "final")

AGENT_PROMPT = """You are PipelineDoctor's investigator agent. A CI/CD build failed and no known
failure rule matched, so you must investigate the log yourself, then give a diagnosis.

Tools (call exactly one per reply):
- search_log       args {{"pattern": "text or regex"}}  -> matching log lines with line numbers (case-insensitive)
- read_lines       args {{"start": N, "end": M}}         -> log lines N..M (at most {max_read} lines)
- search_incidents args {{"query": "some words"}}        -> similar past incidents and how they were fixed
- final            args {{"category": "...", "cause": "one or two plain-English sentences",
                         "fix_hints": ["step 1", "step 2", "step 3"], "confidence": 0.0}}  -> ends the investigation

Facts about the log ({total} lines). Lines that look like errors:
{errors}

Lines around the first error:
{context}

Similar past incidents found so far:
{incidents}

Steps taken so far:
{steps}

Rules:
- If the error lines above already explain the failure, give your final answer straight away.
- Never repeat a tool call you already made. Read the whole range you need in one read_lines call.
- Write everything (thought, cause, fix hints) in English.
- Your final answer must include a cause and at least one concrete fix hint. Use details the log
  shows (who, what, when) and set confidence from how clearly the log supports your cause.
- Fix hints must solve the root cause. Never suggest disabling a safety mechanism (for example
  -lock=false, --force, --no-verify or skipping tests) as the fix, even if the log mentions it.

{budget}
Reply with ONLY a JSON object: {{"thought": "why you are doing this", "action": "<tool name>", "args": {{...}}}}
Be honest: if the log does not explain the failure, say so in "cause" and use a confidence below 0.5."""


class AgentError(Exception):
    """The model's reply could not be turned into a valid action."""


def numbered_error_lines(text: str, errors: List[str], limit: int = 15) -> List[str]:
    """Attach line numbers to the error lines so the agent can read around them."""
    lines = text.splitlines()
    out, start = [], 0
    for err in errors[:limit]:
        for index in range(start, len(lines)):
            if lines[index].strip() == err.strip():
                out.append(f"{index + 1}: {err.strip()[:200]}")
                start = index + 1
                break
        else:
            out.append(f"?: {err.strip()[:200]}")
    return out


def first_error_context(text: str, numbered_errors: List[str]) -> str:
    """The lines around the first error, numbered - often enough to diagnose without any tool."""
    for entry in numbered_errors:
        number = entry.split(":", 1)[0]
        if number.isdigit():
            line = int(number)
            return tool_read_lines(text, max(1, line - CONTEXT_LINES), line + CONTEXT_LINES)
    return "(none)"


def build_agent_prompt(state: Dict[str, Any]) -> str:
    text = state["text"]
    numbered = numbered_error_lines(text, state.get("errors", []))
    errors = numbered or ["(no obvious error lines)"]
    incidents = [
        f"- {inc.id} ({int(score * 100)}% similar): {inc.title}. Fix: {inc.fix}"
        for inc, score in state.get("similar", [])
    ] or ["(none)"]
    trace = state.get("agent_trace", [])
    steps = [
        f"{n}. {step['action']} {json.dumps(step['args'])}\n   result: {step['observation']}"
        for n, step in enumerate(trace, start=1)
    ] or ["(none yet)"]
    left = MAX_TOOL_CALLS - len(trace)
    budget = (
        f"You have {left} tool call(s) left before you must answer."
        if left > 0
        else 'You have no tool calls left: reply with action "final" now.'
    )
    return AGENT_PROMPT.format(
        max_read=MAX_READ_LINES,
        total=len(text.splitlines()),
        errors="\n".join(errors),
        context=first_error_context(text, numbered),
        incidents="\n".join(incidents),
        steps="\n".join(steps),
        budget=budget,
    )


def parse_action(reply: str) -> Tuple[str, Dict[str, Any], str]:
    """Turn a model reply into (action, args, thought). Raises AgentError if invalid."""
    try:
        data = llm.extract_json(reply)
    except llm.LLMUnavailable as exc:
        raise AgentError(str(exc)) from exc
    action = str(data.get("action", "")).strip()
    args = data.get("args") or {}
    thought = str(data.get("thought", "")).strip()[:300]
    # Small models sometimes put the diagnosis at the top level instead of inside "args".
    if action == "final" and not args and "cause" in data:
        args = {k: data[k] for k in ("category", "cause", "fix_hints", "confidence") if k in data}
    if action not in TOOLS:
        raise AgentError(f"unknown action '{action}'; use one of {', '.join(TOOLS)}")
    if not isinstance(args, dict):
        raise AgentError("'args' must be a JSON object")
    return action, args, thought


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OBSERVATION_CHARS else text[: MAX_OBSERVATION_CHARS - 15] + "\n...(truncated)"


def tool_search_log(text: str, pattern: str, limit: int = 10) -> str:
    if not pattern:
        return "error: 'pattern' is required"
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(pattern), re.IGNORECASE)
    hits = [f"{n}: {line.strip()[:200]}" for n, line in enumerate(text.splitlines(), start=1) if rx.search(line)]
    if not hits:
        return f"no lines match '{pattern}'"
    more = f"\n({len(hits) - limit} more matches not shown)" if len(hits) > limit else ""
    return _clip(f"{len(hits)} matching line(s):\n" + "\n".join(hits[:limit]) + more)


def tool_read_lines(text: str, start: Any, end: Any) -> str:
    lines = text.splitlines()
    try:
        start, end = int(start), int(end)
    except (TypeError, ValueError):
        return "error: 'start' and 'end' must be line numbers"
    start = max(1, start)
    end = min(len(lines), end, start + MAX_READ_LINES - 1)
    if start > end:
        return f"error: the log only has {len(lines)} lines"
    return _clip("\n".join(f"{n}: {lines[n - 1][:200]}" for n in range(start, end + 1)))


def tool_search_incidents(kb: KnowledgeBase, query: str) -> str:
    if not query:
        return "error: 'query' is required"
    found = kb.search(query, top_k=3)
    if not found:
        return "no similar past incidents"
    return _clip("\n".join(f"- {inc.id} ({int(score * 100)}% similar): {inc.title}. "
                           f"Root cause: {inc.root_cause} Fix: {inc.fix}" for inc, score in found))


def run_tool(action: str, args: Dict[str, Any], text: str, kb: KnowledgeBase) -> str:
    if action == "search_log":
        return tool_search_log(text, str(args.get("pattern", "")))
    if action == "read_lines":
        return tool_read_lines(text, args.get("start"), args.get("end"))
    if action == "search_incidents":
        return tool_search_incidents(kb, str(args.get("query", "")))
    return f"error: unknown tool '{action}'"


def find_repeat(trace: List[Dict[str, Any]], action: str, args: Dict[str, Any]) -> int:
    """Return the step number of an identical earlier call, or 0 if this call is new."""
    for number, step in enumerate(trace, start=1):
        if step["action"] == action and step["args"] == args:
            return number
    return 0


def summarize_observation(observation: str) -> str:
    """One-line version of a tool result for reports."""
    lines = observation.splitlines() if observation else [""]
    # "3 matching line(s):" alone says little, so include the first hit.
    if lines[0].endswith("matching line(s):") and len(lines) > 1:
        return f"{lines[0]} {lines[1]}"[:160]
    return lines[0][:160]
