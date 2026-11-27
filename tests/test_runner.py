"""Tests for the self-healing `run` command, using small Python scripts as fake builds."""

import json
import sys

from pipelinedoctor.agents import Doctor
from pipelinedoctor.cli import EXIT_INPUT_ERROR, main
from pipelinedoctor.linter import lint_rules
from pipelinedoctor.runner import run_with_healing


def script(tmp_path, body):
    path = tmp_path / "build.py"
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


FLAKY = """
import pathlib, sys
marker = pathlib.Path(__file__).with_name("ran_once")
if not marker.exists():
    marker.write_text("x")
    print("Downloading dependencies...")
    print("error: Connection reset by peer")
    sys.exit(1)
print("BUILD SUCCESS")
"""


def test_success_needs_one_attempt(tmp_path):
    result = run_with_healing(script(tmp_path, "print('BUILD SUCCESS')"), Doctor(), tmp_path / "run.log", backoff=0)
    assert result.succeeded and not result.healed and len(result.attempts) == 1


def test_transient_failure_is_retried_and_healed(tmp_path):
    waits = []
    result = run_with_healing(script(tmp_path, FLAKY), Doctor(), tmp_path / "run.log",
                              backoff=3, sleep=waits.append, use_llm=False)
    assert result.healed and result.exit_code == 0
    assert [a.outcome for a in result.attempts] == ["retried", "succeeded"]
    assert result.attempts[0].rule_id == "TRANSIENT_NETWORK_ERROR"
    assert waits == [3]


def test_non_retryable_failure_is_not_retried(tmp_path):
    body = "import sys\nprint('Foo.java:3: error: cannot find symbol')\nsys.exit(1)\n"
    result = run_with_healing(script(tmp_path, body), Doctor(), tmp_path / "run.log", backoff=0, use_llm=False)
    assert len(result.attempts) == 1 and result.attempts[0].outcome == "stopped"
    assert result.diagnosis.rule_id == "JAVA_COMPILE_ERROR"
    assert "needs a human" in result.attempts[0].detail
    assert result.exit_code == 1


def test_retries_stop_after_the_limit_with_backoff_doubling(tmp_path):
    body = "import sys\nprint('ETIMEDOUT while fetching')\nsys.exit(7)\n"
    waits = []
    result = run_with_healing(script(tmp_path, body), Doctor(), tmp_path / "run.log",
                              max_retries=2, backoff=1, sleep=waits.append, use_llm=False)
    assert [a.outcome for a in result.attempts] == ["retried", "retried", "stopped"]
    assert waits == [1, 2] and result.exit_code == 7


def test_cli_run_passes_exit_code_and_writes_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    cmd = script(tmp_path, FLAKY)
    code = main(["run", "--backoff", "0", "--log", str(tmp_path / "r.log"), "--no-llm",
                 "--github-summary", "--format", "json", "--"] + cmd)
    out = capsys.readouterr().out
    assert code == 0
    data = json.loads(out[out.index("{"):])
    assert data["healed"] is True and data["attempts"][0]["outcome"] == "retried"
    assert "recovered automatically after 1 retry" in summary.read_text(encoding="utf-8")


def test_cli_run_without_command():
    assert main(["run"]) == EXIT_INPUT_ERROR


def test_linter_rejects_non_boolean_retryable(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text("rules:\n  - id: X_RULE\n    category: c\n    severity: low\n    retryable: maybe\n"
                    "    patterns: ['some long pattern']\n    cause: x\n    fix_hints: [a, b]\n", encoding="utf-8")
    assert any("retryable" in i.message and i.level == "error" for i in lint_rules(str(path)))
