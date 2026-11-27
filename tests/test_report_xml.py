import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pipelinedoctor.cli import main

SAMPLES = Path(__file__).resolve().parent.parent / "sample_logs"


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_junit_xml_is_valid_and_counts_results(capsys):
    main([
        "analyze",
        str(SAMPLES / "gha_pytest_failure.log"),
        str(SAMPLES / "unknown_custom_tool.log"),
        "--format", "junit", "--no-llm",
    ])
    root = ET.fromstring(capsys.readouterr().out.split("\n", 1)[1])
    assert root.tag == "testsuite"
    assert root.get("tests") == "2"
    assert root.get("failures") == "1"
    assert root.get("errors") == "1"
    failure = root.find("testcase/failure")
    assert failure.get("type") == "TEST_FAILURE"
    assert "How to fix" in failure.text


def test_junit_escapes_special_characters(tmp_path, capsys):
    log = tmp_path / "special.log"
    log.write_text('Permission denied: <file name="a&b">\n', encoding="utf-8")
    main(["analyze", str(log), "--format", "junit"])
    root = ET.fromstring(capsys.readouterr().out.split("\n", 1)[1])
    assert '<file name="a&b">' in root.find("testcase/failure").text
