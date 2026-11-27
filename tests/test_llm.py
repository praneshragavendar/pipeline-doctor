import pytest

from pipelinedoctor import llm


def test_parse_reply_extracts_json_from_text():
    reply = 'Here you go: {"category": "Network", "cause": "DNS failed", "fix_hints": ["check DNS"], "confidence": 0.7}'
    result = llm.parse_reply(reply)
    assert result["category"] == "Network"
    assert result["fix_hints"] == ["check DNS"]


def test_parse_reply_caps_confidence():
    reply = '{"category": "x", "cause": "y", "fix_hints": [], "confidence": 0.99}'
    assert llm.parse_reply(reply)["confidence"] == 0.8


def test_parse_reply_handles_bad_confidence():
    reply = '{"category": "x", "cause": "y", "fix_hints": "one hint", "confidence": "high"}'
    result = llm.parse_reply(reply)
    assert result["confidence"] == 0.3
    assert result["fix_hints"] == ["one hint"]


def test_parse_reply_rejects_non_json():
    with pytest.raises(llm.LLMUnavailable):
        llm.parse_reply("I think it is a network problem")


def test_diagnose_without_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.LLMUnavailable):
        llm.diagnose(["error"], [])


def test_prompt_contains_errors_and_incidents():
    from pipelinedoctor.models import Incident

    incident = Incident(id="INC-X", title="Old fix", symptoms="s", root_cause="rc", fix="do this")
    prompt = llm.build_prompt(["fatal: boom"], [incident])
    assert "fatal: boom" in prompt and "do this" in prompt
