import pytest

from pipelinedoctor.rules_engine import RulesError, load_rules, match_rules


@pytest.fixture(scope="module")
def rules():
    return load_rules()


def test_bundled_rules_load(rules):
    assert len(rules) >= 10
    assert all(rule.fix_hints for rule in rules)


@pytest.mark.parametrize(
    "line, expected",
    [
        ("Could not establish trust relationship for the SSL/TLS secure channel.", "SSL_CERT_FAILURE"),
        ("The token '&&' is not a valid statement separator in this version.", "POWERSHELL_AND_OPERATOR"),
        ("ModuleNotFoundError: No module named 'requests'", "PYTHON_MODULE_MISSING"),
        ("[ERROR] Could not resolve dependencies for project com.x:y:jar:1.0", "MAVEN_DEPENDENCY_RESOLUTION"),
        ("java.io.IOException: No space left on device", "DISK_SPACE"),
        ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock", "DOCKER_DAEMON"),
        ("CONFLICT (content): Merge conflict in pom.xml", "GIT_MERGE_CONFLICT"),
        ("Exception in thread main java.lang.OutOfMemoryError: Java heap space", "OUT_OF_MEMORY"),
    ],
)
def test_single_line_matches_expected_rule(rules, line, expected):
    matches = match_rules(line, rules)
    assert matches, f"no rule matched: {line}"
    assert matches[0].rule.id == expected


def test_no_match_on_clean_log(rules):
    assert match_rules("Build succeeded.\nAll 42 tests passed.", rules) == []


def test_matching_is_case_insensitive(rules):
    assert match_rules("PERMISSION DENIED while writing file", rules)[0].rule.id == "PERMISSION_DENIED"


def test_missing_rules_file_gives_helpful_error(tmp_path):
    with pytest.raises(RulesError, match="Fix hint"):
        load_rules(str(tmp_path / "nope.yaml"))


def test_invalid_regex_is_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "rules:\n  - id: BAD\n    category: x\n    severity: low\n"
        "    patterns: ['([unclosed']\n    cause: x\n    fix_hints: [x]\n",
        encoding="utf-8",
    )
    with pytest.raises(RulesError, match="invalid regex"):
        load_rules(str(bad))


def test_rules_path_from_environment(tmp_path, monkeypatch):
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        "rules:\n  - id: CUSTOM\n    category: Custom\n    severity: low\n"
        "    patterns: ['banana error']\n    cause: test\n    fix_hints: [peel it]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PIPELINEDOCTOR_RULES", str(custom))
    loaded = load_rules()
    assert [r.id for r in loaded] == ["CUSTOM"]
