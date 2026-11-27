from pipelinedoctor.knowledge import KnowledgeBase, load_incidents, tokenize


def test_tokenize_drops_stopwords():
    assert tokenize("The build FAILED on the agent") == ["build", "failed", "agent"]


def test_bundled_incidents_load():
    incidents = load_incidents()
    assert len(incidents) >= 5


def test_search_finds_ssl_incident():
    kb = KnowledgeBase(load_incidents())
    results = kb.search("remote certificate is invalid ssl tls secure channel agent")
    assert results
    assert results[0][0].id == "INC-002"


def test_search_finds_msvc_incident():
    kb = KnowledgeBase(load_incidents())
    results = kb.search("MSVC toolset version mismatch vcvars compiler")
    assert results[0][0].id == "INC-001"


def test_empty_knowledge_base_returns_nothing():
    assert KnowledgeBase([]).search("anything") == []


def test_missing_kb_file_returns_empty(tmp_path):
    assert load_incidents(str(tmp_path / "missing.yaml")) == []
