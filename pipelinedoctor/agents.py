"""Agent workflow built with LangGraph.

Each node is a small "agent" with one job. They share a state dictionary and
LangGraph decides which node runs next:

    analyzer -> rule_matcher -> retriever --(rule matched?)--> rule_advisor ---> reporter
                                          \\-(no match)-----> llm_advisor  ---/
"""

from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from pipelinedoctor import llm
from pipelinedoctor.knowledge import KnowledgeBase, load_incidents
from pipelinedoctor.log_reader import error_lines, read_log, tail
from pipelinedoctor.models import Diagnosis, Rule, RuleMatch
from pipelinedoctor.rules_engine import load_rules, match_rules


class DoctorState(TypedDict, total=False):
    """Shared state passed between the agents."""

    log_file: str
    use_llm: bool
    text: str
    errors: List[str]
    matches: List[RuleMatch]
    similar: List[Any]
    llm_result: Optional[Dict[str, Any]]
    llm_error: Optional[str]
    diagnosis: Diagnosis


def build_graph(rules: List[Rule], kb: KnowledgeBase):
    """Wire the agents together and return a compiled LangGraph app."""

    def analyzer(state: DoctorState) -> DoctorState:
        # Agent 1: read the log and pick out the lines that look like errors.
        text = read_log(state["log_file"])
        errors = error_lines(text) or tail(text, 15)
        return {"text": text, "errors": errors}

    def rule_matcher(state: DoctorState) -> DoctorState:
        # Agent 2: check the log against known failure patterns.
        return {"matches": match_rules(state["text"], rules)}

    def retriever(state: DoctorState) -> DoctorState:
        # Agent 3: find similar past incidents (retrieval step of RAG).
        query_parts = list(state["errors"])
        if state["matches"]:
            top = state["matches"][0].rule
            query_parts.append(f"{top.category} {top.id.replace('_', ' ')}")
        return {"similar": kb.search(" ".join(query_parts))}

    def route(state: DoctorState) -> str:
        # Decide: known pattern -> rule advisor; unknown -> LLM advisor.
        return "rule_advisor" if state["matches"] else "llm_advisor"

    def rule_advisor(state: DoctorState) -> DoctorState:
        # Agent 4a: build the diagnosis from the best matching rule.
        best = state["matches"][0]
        confidence = 0.9 if len(best.evidence) > 1 else 0.8
        diagnosis = Diagnosis(
            log_file=state["log_file"],
            status="diagnosed",
            category=best.rule.category,
            severity=best.rule.severity,
            rule_id=best.rule.id,
            cause=best.rule.cause,
            fix_hints=list(best.rule.fix_hints),
            evidence=best.evidence,
            confidence=confidence,
            source="rules",
            similar_incidents=_incident_summaries(state["similar"]),
            other_matches=[m.rule.id for m in state["matches"][1:4]],
        )
        return {"diagnosis": diagnosis}

    def llm_advisor(state: DoctorState) -> DoctorState:
        # Agent 4b: ask the LLM, grounded with similar incidents (generation step of RAG).
        incidents = [incident for incident, _ in state["similar"]]
        if state.get("use_llm", True) and llm.is_configured():
            try:
                return {"llm_result": llm.diagnose(state["errors"], incidents), "llm_error": None}
            except llm.LLMUnavailable as exc:
                return {"llm_result": None, "llm_error": str(exc)}
        reason = "LLM disabled (--no-llm)" if not state.get("use_llm", True) else "ANTHROPIC_API_KEY not set"
        return {"llm_result": None, "llm_error": reason}

    def reporter(state: DoctorState) -> DoctorState:
        # Agent 5: turn whatever we have into the final Diagnosis.
        if "diagnosis" in state and state["diagnosis"] is not None:
            return {}
        similar = _incident_summaries(state["similar"])
        result = state.get("llm_result")
        if result:
            diagnosis = Diagnosis(
                log_file=state["log_file"],
                status="diagnosed",
                category=result["category"],
                severity="unknown",
                rule_id=None,
                cause=result["cause"],
                fix_hints=result["fix_hints"],
                evidence=state["errors"][:3],
                confidence=result["confidence"],
                source="llm",
                similar_incidents=similar,
            )
        else:
            hints = [f"Similar past fix ({s['id']}): {s['fix']}" for s in similar]
            hints.append("Read the first error line in the log; later errors are often side effects.")
            if state.get("llm_error"):
                hints.append(f"For an AI-generated diagnosis, enable the LLM fallback ({state['llm_error']}).")
            diagnosis = Diagnosis(
                log_file=state["log_file"],
                status="unknown",
                category="Unknown",
                severity="unknown",
                rule_id=None,
                cause="No known failure pattern matched this log.",
                fix_hints=hints,
                evidence=state["errors"][:3],
                confidence=0.0,
                source="none",
                similar_incidents=similar,
            )
        return {"diagnosis": diagnosis}

    graph = StateGraph(DoctorState)
    graph.add_node("analyzer", analyzer)
    graph.add_node("rule_matcher", rule_matcher)
    graph.add_node("retriever", retriever)
    graph.add_node("rule_advisor", rule_advisor)
    graph.add_node("llm_advisor", llm_advisor)
    graph.add_node("reporter", reporter)

    graph.add_edge(START, "analyzer")
    graph.add_edge("analyzer", "rule_matcher")
    graph.add_edge("rule_matcher", "retriever")
    graph.add_conditional_edges("retriever", route, {"rule_advisor": "rule_advisor", "llm_advisor": "llm_advisor"})
    graph.add_edge("rule_advisor", "reporter")
    graph.add_edge("llm_advisor", "reporter")
    graph.add_edge("reporter", END)
    return graph.compile()


def _incident_summaries(similar) -> List[dict]:
    return [
        {"id": i.id, "title": i.title, "fix": i.fix, "similarity": round(score, 2)}
        for i, score in similar
    ]


class Doctor:
    """Convenience wrapper: load rules + knowledge base once, diagnose many logs."""

    def __init__(self, rules_file: Optional[str] = None, kb_file: Optional[str] = None):
        self.rules = load_rules(rules_file)
        self.kb = KnowledgeBase(load_incidents(kb_file))
        self.app = build_graph(self.rules, self.kb)

    def diagnose(self, log_file: str, use_llm: bool = True) -> Diagnosis:
        final_state = self.app.invoke({"log_file": log_file, "use_llm": use_llm})
        return final_state["diagnosis"]
