import pytest


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    # Tests must never call a real LLM: clear every setting that would enable one.
    for name in ("ANTHROPIC_API_KEY", "PIPELINEDOCTOR_LLM", "OLLAMA_HOST", "PIPELINEDOCTOR_OLLAMA_MODEL"):
        monkeypatch.delenv(name, raising=False)
