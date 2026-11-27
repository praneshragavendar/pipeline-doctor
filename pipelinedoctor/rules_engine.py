"""Rule engine: matches a log against known failure patterns from rules.yaml."""

import os
import re
from pathlib import Path
from typing import List, Optional

import yaml

from pipelinedoctor.models import Rule, RuleMatch

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_RULES = DATA_DIR / "rules.yaml"

# Severity adds a little weight so a high-severity match wins a tie.
SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}


class RulesError(Exception):
    """Raised when the rules file is missing or invalid."""


def rules_path(explicit: Optional[str] = None) -> Path:
    """Pick the rules file: CLI flag, then env var, then the bundled default."""
    chosen = explicit or os.environ.get("PIPELINEDOCTOR_RULES")
    return Path(chosen) if chosen else DEFAULT_RULES


def load_rules(path: Optional[str] = None) -> List[Rule]:
    """Load and validate rules from YAML."""
    file_path = rules_path(path)
    if not file_path.is_file():
        raise RulesError(
            f"Rules file not found: {file_path}. "
            "Fix hint: pass --rules <file> or set PIPELINEDOCTOR_RULES."
        )
    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RulesError(f"Rules file {file_path} is not valid YAML: {exc}") from exc

    rules = []
    required = {"id", "category", "severity", "patterns", "cause", "fix_hints"}
    for index, item in enumerate(data.get("rules", [])):
        missing = required - set(item)
        if missing:
            raise RulesError(
                f"Rule #{index + 1} in {file_path} is missing: {', '.join(sorted(missing))}"
            )
        # Fail early on a bad regex instead of crashing in the middle of a scan.
        for pattern in item["patterns"]:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise RulesError(
                    f"Rule {item['id']} has an invalid regex '{pattern}': {exc}"
                ) from exc
        rules.append(
            Rule(
                id=item["id"],
                category=item["category"],
                severity=item["severity"],
                priority=int(item.get("priority", 50)),
                patterns=item["patterns"],
                cause=" ".join(item["cause"].split()),
                fix_hints=item["fix_hints"],
            )
        )
    if not rules:
        raise RulesError(f"No rules found in {file_path}.")
    return rules


def match_rules(text: str, rules: List[Rule], max_evidence: int = 3) -> List[RuleMatch]:
    """Return every rule that matches the log, best match first."""
    lines = text.splitlines()
    matches = []
    for rule in rules:
        compiled = [re.compile(p, re.IGNORECASE) for p in rule.patterns]
        evidence = []
        for line in lines:
            if any(rx.search(line) for rx in compiled):
                evidence.append(line.strip())
        if evidence:
            score = rule.priority + SEVERITY_WEIGHT.get(rule.severity, 1) + min(len(evidence), 5)
            matches.append(RuleMatch(rule=rule, evidence=evidence[:max_evidence], score=score))
    return sorted(matches, key=lambda m: m.score, reverse=True)
