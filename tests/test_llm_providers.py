import json

import pytest

from pipelinedoctor import llm


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def capture(monkeypatch, payload):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return FakeResponse(payload)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_provider_selection(monkeypatch):
    assert llm.provider() is None and llm.describe() == "none"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert llm.provider() == "anthropic"
    monkeypatch.setenv("PIPELINEDOCTOR_LLM", "ollama")
    assert llm.provider() == "ollama"
    assert llm.describe() == f"ollama:{llm.DEFAULT_OLLAMA_MODEL}"
    monkeypatch.setenv("PIPELINEDOCTOR_LLM", "off")
    assert llm.provider() is None


def test_ollama_request_is_local_and_forces_json(monkeypatch):
    monkeypatch.setenv("PIPELINEDOCTOR_LLM", "ollama")
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    seen = capture(monkeypatch, {"message": {"content": '{"action": "final"}'}})
    assert llm.chat("hello") == '{"action": "final"}'
    assert seen["url"] == "http://127.0.0.1:11434/api/chat"
    assert seen["body"]["format"] == "json" and seen["body"]["stream"] is False
    assert seen["body"]["model"] == llm.DEFAULT_OLLAMA_MODEL
    assert seen["timeout"] == 300


def test_anthropic_request_uses_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    seen = capture(monkeypatch, {"content": [{"type": "text", "text": "hi"}]})
    assert llm.chat("hello") == "hi"
    assert seen["url"] == llm.ANTHROPIC_URL
    assert seen["headers"].get("X-api-key") == "secret"


def test_empty_ollama_reply_raises(monkeypatch):
    monkeypatch.setenv("PIPELINEDOCTOR_LLM", "ollama")
    capture(monkeypatch, {"message": {"content": ""}})
    with pytest.raises(llm.LLMUnavailable):
        llm.chat("hello")


def test_chat_without_provider_raises():
    with pytest.raises(llm.LLMUnavailable):
        llm.chat("hello")


def test_network_error_becomes_llm_unavailable(monkeypatch):
    monkeypatch.setenv("PIPELINEDOCTOR_LLM", "ollama")

    def refuse(request, timeout):
        raise llm.urllib.error.URLError("connection refused")

    monkeypatch.setattr(llm.urllib.request, "urlopen", refuse)
    with pytest.raises(llm.LLMUnavailable):
        llm.chat("hello")
