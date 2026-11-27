r"""Agent workflow built with LangGraph.

Each node is a small "agent" with one job. They share a state dictionary and
LangGraph decides which node runs next:

    analyzer -> rule_matcher -> retriever --(rule matched)--> rule_advisor ------------------> reporter
                                          \--(no match, LLM on)--> investigator <--> tools        ^
                                          |                         \--(bad replies)--> llm_advisor
                                          \--(no match, no LLM)-----------------------------------/

The investigator is the agentic part: an LLM that chooses its own next step (search the log,
read lines, search past incidents) in a loop until it can give a diagnosis.
"""

from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from pipelinedoctor import investigator, llm
from pipelinedoctor.knowledge import KnowledgeBase, load_incidents
from pipelinedoctor.log_reader import error_lines, read_log, tail
from pipelinedoctor.models import Diagnosis, Rule, RuleMatch
from pipelinedoctor.rules_engine import load_rules, match_rules

MAX_BAD_REPLIES = 2


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
    agent_trace: List[Dict[str, Any]]
    pending: Optional[Dict[str, Any]]
    agent_result: Optional[Dict[str, Any]]
    agent_status: str  # "running", "done", "bad_replies", "unavailable"
    bad_replies: int
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
        # Known pattern -> rule advisor. Unknown -> investigator agent if an LLM is available.
        if state["matches"]:
            return "rule_advisor"
        if state.get("use_llm", True) and llm.is_configured():
            return "investigator"
        return "reporter"

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

    def investigator_node(state: DoctorState) -> DoctorState:
        # Agent 4b: the LLM looks at what it knows so far and picks its next action.
        trace = state.get("agent_trace", [])
        bad = state.get("bad_replies", 0)
        try:
            reply = llm.chat(investigator.build_agent_prompt(state))
        except llm.LLMUnavailable as exc:
            return {"agent_status": "unavailable", "llm_error": str(exc), "pending": None}
        try:
            action, args, thought = investigator.parse_action(reply)
            if action != "final" and len(trace) >= investigator.MAX_TOOL_CALLS:
                raise investigator.AgentError("no tool calls left; a final answer was required")
        except investigator.AgentError as exc:
            bad += 1
            status = "bad_replies" if bad >= MAX_BAD_REPLIES else "running"
            return {"bad_replies": bad, "agent_status": status, "llm_error": f"agent reply rejected: {exc}", "pending": None}
        if action == "final":
            result = llm.normalize_diagnosis(args)
            return {"agent_result": result, "agent_status": "done", "pending": None,
                    "agent_trace": trace + [{"thought": thought, "action": "final", "args": {}, "observation": "diagnosis given"}]}
        return {"pending": {"action": action, "args": args, "thought": thought}, "agent_status": "running"}

    def tools_node(state: DoctorState) -> DoctorState:
        # Runs the tool the investigator asked for (read-only) and records what it saw.
        call = state["pending"]
        trace = state.get("agent_trace", [])
        repeat = investigator.find_repeat(trace, call["action"], call["args"])
        if repeat:
            # Small models sometimes loop; don't re-run the tool, push the agent forward instead.
            observation = (f"You already ran this exact call in step {repeat}; its result is above. "
                           "Use a different tool or give your final answer.")
        else:
            observation = investigator.run_tool(call["action"], call["args"], state["text"], kb)
        step = {"thought": call["thought"], "action": call["action"], "args": call["args"], "observation": observation}
        return {"agent_trace": state.get("agent_trace", []) + [step], "pending": None}

    def after_investigator(state: DoctorState) -> str:
        status = state.get("agent_status")
        if state.get("pending"):
            return "tools"
        if status == "running":
            return "investigator"   # the model gave a bad reply; let it try once more
        if status == "bad_replies":
            return "llm_advisor"    # agent loop failed; fall back to a single direct question
        return "reporter"

    def llm_advisor(state: DoctorState) -> DoctorState:
        # Fallback: ask the LLM once, grounded with similar incidents (generation step of RAG).
        incidents = [incident for incident, _ in state["similar"]]
        try:
            return {"llm_result": llm.diagnose(state["errors"], incidents), "llm_error": None}
        except llm.LLMUnavailable as exc:
            return {"llm_result": None, "llm_error": str(exc)}

    def reporter(state: DoctorState) -> DoctorState:
        # Agent 5: turn whatever we have into the final Diagnosis.
        if "diagnosis" in state and state["diagnosis"] is not None:
            return {}
        similar = _incident_summaries(state["similar"])
        trace = _trace_for_report(state.get("agent_trace", []))
        result = state.get("agent_result") or state.get("llm_result")
        if result:
            source = "agent" if state.get("agent_result") else "llm"
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
                source=source,
                similar_incidents=similar,
                llm=llm.describe(),
                agent_trace=trace,
            )
        else:
            hints = [f"Similar past fix ({s['id']}): {s['fix']}" for s in similar]
            hints.append("Read the first error line in the log; later errors are often side effects.")
            if not state.get("use_llm", True):
                reason = "LLM disabled (--no-llm)"
            elif not llm.is_configured():
                reason = "set PIPELINEDOCTOR_LLM=ollama for a free local model, or ANTHROPIC_API_KEY"
            else:
                reason = state.get("llm_error") or "the LLM gave no usable answer"
            hints.append(f"For an AI investigation, enable the LLM ({reason}).")
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
                agent_trace=trace,
            )
        return {"diagnosis": diagnosis}

    graph = StateGraph(DoctorState)
    graph.add_node("analyzer", analyzer)
    graph.add_node("rule_matcher", rule_matcher)
    graph.add_node("retriever", retriever)
    graph.add_node("rule_advisor", rule_advisor)
    graph.add_node("investigator", investigator_node)
    graph.add_node("tools", tools_node)
    graph.add_node("llm_advisor", llm_advisor)
    graph.add_node("reporter", reporter)

    graph.add_edge(START, "analyzer")
    graph.add_edge("analyzer", "rule_matcher")
    graph.add_edge("rule_matcher", "retriever")
    graph.add_conditional_edges(
        "retriever", route,
        {"rule_advisor": "rule_advisor", "investigator": "investigator", "reporter": "reporter"},
    )
    graph.add_conditional_edges(
        "investigator", after_investigator,
        {"tools": "tools", "investigator": "investigator", "llm_advisor": "llm_advisor", "reporter": "reporter"},
    )
    # The agent loop: after every tool call, control goes back to the investigator.
    graph.add_edge("tools", "investigator")
    graph.add_edge("rule_advisor", "reporter")
    graph.add_edge("llm_advisor", "reporter")
    graph.add_edge("reporter", END)
    return graph.compile()


def _trace_for_report(trace: List[Dict[str, Any]]) -> List[dict]:
    return [
        {"step": n, "action": t["action"], "args": t["args"], "thought": t["thought"],
         "result": investigator.summarize_observation(t["observation"])}
        for n, t in enumerate(trace, start=1)
    ]


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
        # recursion_limit bounds the agent loop even if something goes wrong (each step = 1 node).
        final_state = self.app.invoke({"log_file": log_file, "use_llm": use_llm}, {"recursion_limit": 60})
        return final_state["diagnosis"]
