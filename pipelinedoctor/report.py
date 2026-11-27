"""Formatting a Diagnosis for humans (text) or machines (JSON)."""

import json
import xml.etree.ElementTree as ET
from typing import List

from pipelinedoctor.models import Diagnosis


def to_json(diagnoses: List[Diagnosis]) -> str:
    payload = [d.to_dict() for d in diagnoses]
    return json.dumps(payload[0] if len(payload) == 1 else payload, indent=2)


def to_text(diagnosis: Diagnosis) -> str:
    """Readable report for a terminal or a CI job summary."""
    icon = "[DIAGNOSED]" if diagnosis.status == "diagnosed" else "[UNKNOWN]"
    lines = [
        "=" * 70,
        f"{icon} {diagnosis.log_file}",
        "=" * 70,
        f"Category   : {diagnosis.category}",
        f"Severity   : {diagnosis.severity}",
        f"Confidence : {int(diagnosis.confidence * 100)}%  (source: {diagnosis.source})",
    ]
    if diagnosis.rule_id:
        lines.append(f"Rule       : {diagnosis.rule_id}")
    lines += ["", "Likely cause:", f"  {diagnosis.cause}", "", "How to fix:"]
    lines += [f"  {n}. {hint}" for n, hint in enumerate(diagnosis.fix_hints, start=1)]
    if diagnosis.evidence:
        lines += ["", "Evidence from the log:"]
        lines += [f"  > {line[:150]}" for line in diagnosis.evidence]
    if diagnosis.similar_incidents:
        lines += ["", "Similar past incidents:"]
        lines += [
            f"  - {s['id']} ({int(s['similarity'] * 100)}% similar): {s['title']}"
            for s in diagnosis.similar_incidents
        ]
    if diagnosis.other_matches:
        lines += ["", f"Also matched: {', '.join(diagnosis.other_matches)}"]
    return "\n".join(lines)


def to_markdown(diagnosis: Diagnosis) -> str:
    """Markdown version, used for GitHub Actions job summaries."""
    hints = "\n".join(f"{n}. {h}" for n, h in enumerate(diagnosis.fix_hints, start=1))
    evidence = "\n".join(diagnosis.evidence[:3])
    return (
        f"## PipelineDoctor: {diagnosis.category}\n\n"
        f"**Status:** {diagnosis.status} | **Confidence:** {int(diagnosis.confidence * 100)}% "
        f"| **Source:** {diagnosis.source}\n\n"
        f"**Likely cause:** {diagnosis.cause}\n\n"
        f"**How to fix:**\n{hints}\n\n"
        f"<details><summary>Evidence</summary>\n\n```\n{evidence}\n```\n</details>\n"
    )


def to_junit_xml(diagnoses: List[Diagnosis]) -> str:
    """JUnit XML report so Jenkins (junit step) and other CI tools can display diagnoses.

    Each log becomes a test case: diagnosed failures are reported as <failure> with the
    cause and fix hints; logs that could not be diagnosed are reported as <error>.
    """
    suite = ET.Element(
        "testsuite",
        name="pipelinedoctor",
        tests=str(len(diagnoses)),
        failures=str(sum(d.status == "diagnosed" for d in diagnoses)),
        errors=str(sum(d.status == "unknown" for d in diagnoses)),
    )
    for d in diagnoses:
        case = ET.SubElement(suite, "testcase", classname="pipelinedoctor.diagnosis", name=d.log_file)
        tag = "failure" if d.status == "diagnosed" else "error"
        details = ET.SubElement(case, tag, message=f"{d.category}: {d.cause}", type=d.rule_id or "UNKNOWN")
        details.text = "\n".join(
            [f"Confidence: {int(d.confidence * 100)}% (source: {d.source})", "How to fix:"]
            + [f"  {n}. {h}" for n, h in enumerate(d.fix_hints, start=1)]
            + ["Evidence:"] + [f"  > {line}" for line in d.evidence]
        )
    ET.indent(suite)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode")
