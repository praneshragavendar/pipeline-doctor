"""Data classes shared across the project."""

from dataclasses import asdict, dataclass, field
from typing import List, Optional


@dataclass
class Rule:
    """One known failure pattern loaded from rules.yaml."""

    id: str
    category: str
    severity: str
    priority: int
    patterns: List[str]
    cause: str
    fix_hints: List[str]
    retryable: bool = False  # transient failure: safe for `pipelinedoctor run` to retry automatically


@dataclass
class RuleMatch:
    """A rule that matched the log, with the evidence lines."""

    rule: Rule
    evidence: List[str]
    score: int


@dataclass
class Incident:
    """A past incident from the knowledge base."""

    id: str
    title: str
    symptoms: str
    root_cause: str
    fix: str
    tags: List[str] = field(default_factory=list)


@dataclass
class Diagnosis:
    """Final result returned to the user."""

    log_file: str
    status: str  # "diagnosed" or "unknown"
    category: str
    severity: str
    rule_id: Optional[str]
    cause: str
    fix_hints: List[str]
    evidence: List[str]
    confidence: float
    source: str  # "rules", "agent", "llm" or "none"
    similar_incidents: List[dict] = field(default_factory=list)
    other_matches: List[str] = field(default_factory=list)
    llm: str = ""  # e.g. "ollama:qwen2.5:3b" when an LLM produced the diagnosis
    agent_trace: List[dict] = field(default_factory=list)  # the investigator's steps

    def to_dict(self) -> dict:
        return asdict(self)
