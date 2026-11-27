"""Config linter: validates rules and knowledge-base YAML files.

Designed to run as a CI gate (Jenkins / GitHub Actions) so a broken rule or
incident file is caught before it is merged. Every problem comes with a fix hint.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

import yaml

ALLOWED_SEVERITIES = {"high", "medium", "low"}
ID_FORMAT = re.compile(r"^[A-Z][A-Z0-9_]*$")
INCIDENT_ID_FORMAT = re.compile(r"^INC-\d{3,}$")


@dataclass
class LintIssue:
    file: str
    location: str
    level: str  # "error" or "warning"
    message: str
    fix_hint: str

    def __str__(self) -> str:
        return f"{self.file}: {self.location}: {self.level}: {self.message}\n    fix hint: {self.fix_hint}"


def _load_yaml(path: Path, issues: List[LintIssue]):
    if not path.is_file():
        issues.append(LintIssue(str(path), "-", "error", "file not found", "check the path you passed to lint"))
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}" if mark else "-"
        issues.append(
            LintIssue(str(path), where, "error", f"invalid YAML: {getattr(exc, 'problem', exc)}",
                      "check indentation (spaces, not tabs) and quotes around special characters")
        )
        return None


def lint_rules(path: str) -> List[LintIssue]:
    """Validate a rules YAML file."""
    file_path = Path(path)
    issues: List[LintIssue] = []
    data = _load_yaml(file_path, issues)
    if data is None:
        return issues

    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        return [LintIssue(path, "rules", "error", "no 'rules:' list found", "the file must start with 'rules:' followed by a list")]

    seen_ids = set()
    for index, rule in enumerate(rules, start=1):
        where = f"rule #{index}"
        if not isinstance(rule, dict):
            issues.append(LintIssue(path, where, "error", "rule is not a mapping", "each rule needs keys like id, category, patterns"))
            continue
        rule_id = rule.get("id")
        if rule_id:
            where = f"rule {rule_id}"
        for key in ("id", "category", "severity", "patterns", "cause", "fix_hints"):
            if not rule.get(key):
                issues.append(LintIssue(path, where, "error", f"missing '{key}'", f"add a '{key}:' field to this rule"))
        if rule_id and not ID_FORMAT.match(str(rule_id)):
            issues.append(LintIssue(path, where, "warning", f"id '{rule_id}' is not UPPER_SNAKE_CASE", "rename it, e.g. MY_RULE_NAME"))
        if rule_id in seen_ids:
            issues.append(LintIssue(path, where, "error", f"duplicate id '{rule_id}'", "every rule id must be unique"))
        seen_ids.add(rule_id)

        severity = rule.get("severity")
        if severity and severity not in ALLOWED_SEVERITIES:
            issues.append(LintIssue(path, where, "error", f"severity '{severity}' is not allowed",
                                    f"use one of: {', '.join(sorted(ALLOWED_SEVERITIES))}"))
        priority = rule.get("priority", 50)
        if not isinstance(priority, int) or not 0 <= priority <= 100:
            issues.append(LintIssue(path, where, "error", f"priority '{priority}' must be an integer 0-100", "set e.g. priority: 60"))

        patterns = rule.get("patterns") or []
        if not isinstance(patterns, list):
            issues.append(LintIssue(path, where, "error", "'patterns' must be a list", "write each pattern on its own '- ' line"))
            patterns = []
        for pattern in patterns:
            try:
                compiled = re.compile(str(pattern))
            except re.error as exc:
                issues.append(LintIssue(path, where, "error", f"invalid regex '{pattern}': {exc}",
                                        "escape special characters like ( ) [ ] . * + ? with a backslash"))
                continue
            if compiled.search(""):
                issues.append(LintIssue(path, where, "error", f"regex '{pattern}' matches an empty string",
                                        "a pattern that matches nothing-at-all matches every line; make it more specific"))
            elif len(str(pattern)) < 6:
                issues.append(LintIssue(path, where, "warning", f"regex '{pattern}' is very short",
                                        "short patterns cause false positives; include more of the error text"))

        if "retryable" in rule and not isinstance(rule["retryable"], bool):
            issues.append(LintIssue(path, where, "error", f"retryable '{rule['retryable']}' must be true or false",
                                    "write 'retryable: true' only for temporary failures that pass on a re-run"))

        hints = rule.get("fix_hints") or []
        if isinstance(hints, list) and len(hints) < 2:
            issues.append(LintIssue(path, where, "warning", "only one fix hint", "give at least two concrete things to try"))
    return issues


def lint_incidents(path: str) -> List[LintIssue]:
    """Validate a knowledge-base YAML file."""
    issues: List[LintIssue] = []
    data = _load_yaml(Path(path), issues)
    if data is None:
        return issues
    incidents = data.get("incidents")
    if not isinstance(incidents, list) or not incidents:
        return [LintIssue(path, "incidents", "error", "no 'incidents:' list found", "the file must start with 'incidents:' followed by a list")]

    seen = set()
    for index, incident in enumerate(incidents, start=1):
        where = f"incident {incident.get('id', '#' + str(index))}" if isinstance(incident, dict) else f"incident #{index}"
        if not isinstance(incident, dict):
            issues.append(LintIssue(path, where, "error", "incident is not a mapping", "each incident needs id, title, symptoms, root_cause, fix"))
            continue
        for key in ("id", "title", "symptoms", "root_cause", "fix"):
            if not incident.get(key):
                issues.append(LintIssue(path, where, "error", f"missing '{key}'", f"add a '{key}:' field"))
        incident_id = incident.get("id")
        if incident_id and not INCIDENT_ID_FORMAT.match(str(incident_id)):
            issues.append(LintIssue(path, where, "warning", f"id '{incident_id}' should look like INC-001", "use the INC-### format"))
        if incident_id in seen:
            issues.append(LintIssue(path, where, "error", f"duplicate id '{incident_id}'", "every incident id must be unique"))
        seen.add(incident_id)
        if not incident.get("tags"):
            issues.append(LintIssue(path, where, "warning", "no tags", "add tags; they improve search accuracy"))
    return issues
