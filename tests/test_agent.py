"""Tests for the investigator agent loop, using a scripted fake LLM (no real model)."""

import json
from pathlib import Path

import pytest

from pipelinedoctor import investigator, llm
from pipelinedoctor.agents import Doctor
from pipelinedoctor.knowledge import KnowledgeBase, load_incidents
from pipelinedoctor.report import to_markdown, to_text

SAMPLES = Path(__file__).resolve().parent.parent / "sample_logs"
UNKNOWN = str(SAMPLES / "unknown_custom_tool.log")


def scripted(monkeypatch, replies):
    """Make llm.chat return the given replies in order and record the prompts it was sent."""
    monkeypatch.setenv("PIPELINEDOCTOR_LLM", "ollama")
    prompts = []
    queue = list(replies)

    def fake_chat(prompt, timeout=None):
        prompts.append(prompt)
        reply = queue.pop(0) if queue else queue_last[0]
        return reply if isinstance(reply, str) else json.dumps(reply)

    queue_last = [replies[-1] if isinstance(replies[-1], str) else json.dumps(replies[-1])]
    monkeypatch.setattr(llm, "chat", fake_chat)
    return prompts


def test_agent_investigates_with_tools_then_diagnoses(monkeypatch):
    prompts = scripted(monkeypatch, [
        {"thought": "find the status code", "action": "search_log", "args": {"pattern": "QX-417"}},
        {"thought": "read around it", "action": "read_lines", "args": {"start": 3, "end": 6}},
        {"thought": "done", "action": "final", "args": {
            "category": "Deployment / Asset sync", "cause": "sync-assets returned QX-417 and triggered a rollback.",
            "fix_hints": ["Look up QX-417 in deploy-helper docs", "Re-run sync-assets with verbose logs"],
            "confidence": 0.95}},
    ])
    diagnosis = Doctor().diagnose(UNKNOWN)

    assert diagnosis.status == "diagnosed"
    assert diagnosis.source == "agent"
    assert diagnosis.llm.startswith("ollama:")
    assert diagnosis.confidence == llm.MAX_LLM_CONFIDENCE  # 0.95 is capped
    assert [s["action"] for s in diagnosis.agent_trace] == ["search_log", "read_lines", "final"]
    assert "QX-417" in diagnosis.agent_trace[0]["result"]
    # The agent sees the result of each tool call in its next prompt.
    assert "QX-417" in prompts[1] and "rollback triggered" in prompts[2]


def test_agent_must_answer_when_tool_budget_is_used(monkeypatch):
    calls = [{"thought": "look", "action": "search_log", "args": {"pattern": "error"}}] * investigator.MAX_TOOL_CALLS
    final = {"thought": "ok", "action": "final", "args": {"category": "X", "cause": "y", "fix_hints": ["a"], "confidence": 0.4}}
    prompts = scripted(monkeypatch, calls + [final])
    diagnosis = Doctor().diagnose(UNKNOWN)
    assert diagnosis.source == "agent"
    assert len(diagnosis.agent_trace) == investigator.MAX_TOOL_CALLS + 1
    assert 'reply with action "final" now' in prompts[-1]


def test_agent_that_never_answers_falls_back_to_one_shot(monkeypatch):
    # Always asks for another tool, even after the budget is gone.
    scripted(monkeypatch, [{"thought": "more", "action": "search_log", "args": {"pattern": "x"}}])
    monkeypatch.setattr(llm, "diagnose", lambda e, i: {"category": "C", "cause": "c", "fix_hints": [], "confidence": 0.5})
    diagnosis = Doctor().diagnose(UNKNOWN)
    assert diagnosis.source == "llm"
    assert len(diagnosis.agent_trace) == investigator.MAX_TOOL_CALLS


def test_agent_recovers_from_one_bad_reply(monkeypatch):
    scripted(monkeypatch, [
        "sorry, I am not sure",
        {"thought": "ok", "action": "final", "args": {"category": "C", "cause": "c", "fix_hints": ["h"], "confidence": 0.5}},
    ])
    assert Doctor().diagnose(UNKNOWN).source == "agent"


def test_known_failures_never_call_the_llm(monkeypatch):
    monkeypatch.setenv("PIPELINEDOCTOR_LLM", "ollama")

    def fail(*a, **k):
        raise AssertionError("LLM must not be called for a rule match")

    monkeypatch.setattr(llm, "chat", fail)
    diagnosis = Doctor().diagnose(str(SAMPLES / "docker_disk_full.log"))
    assert diagnosis.source == "rules" and diagnosis.agent_trace == []


def test_reports_show_the_investigation(monkeypatch):
    scripted(monkeypatch, [
        {"thought": "check | pipes", "action": "search_log", "args": {"pattern": "rollback"}},
        {"thought": "done", "action": "final", "args": {"category": "C", "cause": "c", "fix_hints": ["h"], "confidence": 0.5}},
    ])
    diagnosis = Doctor().diagnose(UNKNOWN)
    assert "How the agent investigated" in to_text(diagnosis)
    markdown = to_markdown(diagnosis)
    assert "| 1 | `search_log`" in markdown and "check \\| pipes" in markdown


def test_parse_action_accepts_top_level_final_and_rejects_unknown_tools():
    action, args, _ = investigator.parse_action('{"action": "final", "cause": "c", "category": "k"}')
    assert action == "final" and args["cause"] == "c"
    with pytest.raises(investigator.AgentError):
        investigator.parse_action('{"action": "rm_rf", "args": {}}')
    with pytest.raises(investigator.AgentError):
        investigator.parse_action("no json here")


def test_tools_are_safe_with_bad_input():
    text = "line one\nERROR (boom) here\nline three"
    assert "2: ERROR (boom) here" in investigator.tool_search_log(text, "(boom")  # invalid regex is escaped
    assert "no lines match" in investigator.tool_search_log(text, "missing")
    assert investigator.tool_read_lines(text, 2, 999).splitlines() == ["2: ERROR (boom) here", "3: line three"]
    assert "must be line numbers" in investigator.tool_read_lines(text, "a", "b")
    assert "only has 3 lines" in investigator.tool_read_lines(text, 10, 20)
    kb = KnowledgeBase(load_incidents())
    assert "required" in investigator.tool_search_incidents(kb, "")
    assert "INC-" in investigator.tool_search_incidents(kb, "msvc toolset v143 build tools")


def test_numbered_error_lines():
    text = "ok\nERROR one\nok\nERROR two"
    assert investigator.numbered_error_lines(text, ["ERROR one", "ERROR two"]) == ["2: ERROR one", "4: ERROR two"]
