import json
from pathlib import Path

import pytest

from pipelinedoctor.agents import Doctor
from pipelinedoctor.cli import EXIT_INPUT_ERROR, EXIT_OK, EXIT_UNKNOWN, main

SAMPLES = Path(__file__).resolve().parent.parent / "sample_logs"


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    # Tests must never call a real LLM.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.mark.parametrize(
    "log_name, rule_id",
    [
        ("azure_msvc_toolset.log", "MSVC_TOOLSET_MISMATCH"),
        ("agent_ssl_registration.log", "SSL_CERT_FAILURE"),
        ("powershell_and_operator.log", "POWERSHELL_AND_OPERATOR"),
        ("jenkins_maven_dependency.log", "MAVEN_DEPENDENCY_RESOLUTION"),
        ("gha_pytest_failure.log", "TEST_FAILURE"),
        ("docker_disk_full.log", "DISK_SPACE"),
    ],
)
def test_sample_logs_are_diagnosed(log_name, rule_id):
    diagnosis = Doctor().diagnose(str(SAMPLES / log_name))
    assert diagnosis.status == "diagnosed"
    assert diagnosis.rule_id == rule_id
    assert diagnosis.source == "rules"
    assert diagnosis.fix_hints


def test_msvc_log_retrieves_matching_past_incident():
    diagnosis = Doctor().diagnose(str(SAMPLES / "azure_msvc_toolset.log"))
    assert diagnosis.similar_incidents[0]["id"] == "INC-001"


def test_unknown_log_falls_back_without_llm():
    diagnosis = Doctor().diagnose(str(SAMPLES / "unknown_custom_tool.log"))
    assert diagnosis.status == "unknown"
    assert diagnosis.source == "none"
    assert any("LLM" in hint for hint in diagnosis.fix_hints)


def test_cli_json_output(capsys):
    code = main(["analyze", str(SAMPLES / "agent_ssl_registration.log"), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert data["rule_id"] == "SSL_CERT_FAILURE"


def test_cli_fail_on_unknown_exit_code():
    code = main(["analyze", str(SAMPLES / "unknown_custom_tool.log"), "--no-llm", "--fail-on-unknown"])
    assert code == EXIT_UNKNOWN


def test_cli_missing_file_exit_code(capsys):
    code = main(["analyze", "does_not_exist.log"])
    assert code == EXIT_INPUT_ERROR
    assert "Fix hint" in capsys.readouterr().err


def test_cli_handles_windows_utf16_log(tmp_path, capsys):
    log = tmp_path / "utf16.log"
    log.write_bytes("Permission denied: D:\\agent\\work\r\n".encode("utf-16"))
    code = main(["analyze", str(log), "--format", "json"])
    assert code == EXIT_OK
    assert json.loads(capsys.readouterr().out)["rule_id"] == "PERMISSION_DENIED"


def test_cli_rules_command(capsys):
    assert main(["rules"]) == EXIT_OK
    assert "SSL_CERT_FAILURE" in capsys.readouterr().out


def test_unknown_log_uses_llm_when_configured(monkeypatch):
    from pipelinedoctor import llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake = {"category": "Deployment", "cause": "sync-assets failed", "fix_hints": ["check asset store"], "confidence": 0.6}
    monkeypatch.setattr(llm, "diagnose", lambda errors, incidents: fake)
    diagnosis = Doctor().diagnose(str(SAMPLES / "unknown_custom_tool.log"))
    assert diagnosis.status == "diagnosed"
    assert diagnosis.source == "llm"
    assert diagnosis.confidence == 0.6


def test_llm_failure_degrades_gracefully(monkeypatch):
    from pipelinedoctor import llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def boom(errors, incidents):
        raise llm.LLMUnavailable("network down")

    monkeypatch.setattr(llm, "diagnose", boom)
    diagnosis = Doctor().diagnose(str(SAMPLES / "unknown_custom_tool.log"))
    assert diagnosis.status == "unknown"
    assert any("network down" in hint for hint in diagnosis.fix_hints)
