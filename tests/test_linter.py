from pipelinedoctor.cli import EXIT_LINT_FAILED, EXIT_OK, main
from pipelinedoctor.knowledge import DEFAULT_KB
from pipelinedoctor.linter import lint_incidents, lint_rules
from pipelinedoctor.rules_engine import DEFAULT_RULES


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_bundled_files_are_clean():
    assert lint_rules(str(DEFAULT_RULES)) == []
    assert lint_incidents(str(DEFAULT_KB)) == []


def test_duplicate_rule_ids_are_errors(tmp_path):
    rule = (
        "  - id: SAME\n    category: c\n    severity: low\n    patterns: ['some long pattern']\n"
        "    cause: x\n    fix_hints: [a, b]\n"
    )
    issues = lint_rules(write(tmp_path, "r.yaml", "rules:\n" + rule + rule))
    assert any("duplicate id" in i.message and i.level == "error" for i in issues)


def test_bad_severity_and_priority(tmp_path):
    text = (
        "rules:\n  - id: X_RULE\n    category: c\n    severity: urgent\n    priority: 500\n"
        "    patterns: ['some long pattern']\n    cause: x\n    fix_hints: [a, b]\n"
    )
    messages = [i.message for i in lint_rules(write(tmp_path, "r.yaml", text))]
    assert any("severity 'urgent'" in m for m in messages)
    assert any("priority '500'" in m for m in messages)


def test_regex_matching_empty_string_is_rejected(tmp_path):
    text = (
        "rules:\n  - id: GREEDY\n    category: c\n    severity: low\n    patterns: ['.*']\n"
        "    cause: x\n    fix_hints: [a, b]\n"
    )
    issues = lint_rules(write(tmp_path, "r.yaml", text))
    assert any("empty string" in i.message for i in issues)


def test_invalid_yaml_reports_line_and_hint(tmp_path):
    issues = lint_rules(write(tmp_path, "r.yaml", "rules:\n  - id: A\n   bad: [unclosed\n"))
    assert issues[0].level == "error"
    assert "invalid YAML" in issues[0].message
    assert issues[0].fix_hint


def test_missing_incident_fields(tmp_path):
    issues = lint_incidents(write(tmp_path, "kb.yaml", "incidents:\n  - id: INC-001\n    title: t\n"))
    missing = {i.message for i in issues if i.level == "error"}
    assert {"missing 'symptoms'", "missing 'root_cause'", "missing 'fix'"} <= missing


def test_lint_cli_exit_codes(tmp_path, capsys):
    assert main(["lint"]) == EXIT_OK
    bad = write(tmp_path, "r.yaml", "rules: []\n")
    assert main(["lint", "--rules", bad]) == EXIT_LINT_FAILED
    assert "fix hint" in capsys.readouterr().out


def test_strict_mode_fails_on_warnings(tmp_path):
    text = (
        "rules:\n  - id: lowercase_id\n    category: c\n    severity: low\n    patterns: ['some long pattern']\n"
        "    cause: x\n    fix_hints: [a, b]\n"
    )
    path = write(tmp_path, "r.yaml", text)
    assert main(["lint", "--rules", path]) == EXIT_OK
    assert main(["lint", "--rules", path, "--strict"]) == EXIT_LINT_FAILED
