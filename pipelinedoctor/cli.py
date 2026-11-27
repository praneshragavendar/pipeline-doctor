"""Command-line interface.

Examples:
    pipelinedoctor analyze build.log
    pipelinedoctor analyze logs/*.log --format json
    pipelinedoctor analyze build.log --no-llm --fail-on-unknown
    pipelinedoctor analyze build.log --format junit > diagnosis.xml
    PIPELINEDOCTOR_LLM=ollama pipelinedoctor analyze build.log   # free local AI investigator
    pipelinedoctor run --max-retries 2 -- mvn -B package          # self-healing: retries transient failures
    pipelinedoctor lint
    pipelinedoctor rules
"""

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from pipelinedoctor import __version__
from pipelinedoctor.agents import Doctor
from pipelinedoctor.log_reader import LogReadError
from pipelinedoctor.linter import lint_incidents, lint_rules
from pipelinedoctor.report import to_json, to_junit_xml, to_markdown, to_text
from pipelinedoctor.rules_engine import DEFAULT_RULES, RulesError, load_rules
from pipelinedoctor.knowledge import DEFAULT_KB
from pipelinedoctor.runner import run_summary_markdown, run_with_healing

# Exit codes so CI pipelines can react to the result.
EXIT_OK = 0
EXIT_UNKNOWN = 2
EXIT_INPUT_ERROR = 3
EXIT_LINT_FAILED = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipelinedoctor",
        description="Diagnose failed CI/CD build logs and suggest fixes.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="Diagnose one or more build log files.")
    analyze.add_argument("logs", nargs="+", help="Path(s) to build log files.")
    analyze.add_argument("--format", choices=["text", "json", "markdown", "junit"], default="text")
    analyze.add_argument("--rules", help="Custom rules YAML (default: bundled rules).")
    analyze.add_argument("--kb", help="Custom knowledge base YAML (default: bundled incidents).")
    analyze.add_argument("--no-llm", action="store_true", help="Never call the LLM fallback.")
    analyze.add_argument(
        "--fail-on-unknown",
        action="store_true",
        help=f"Exit with code {EXIT_UNKNOWN} if any log could not be diagnosed.",
    )
    analyze.add_argument("--github-summary", action="store_true",
                         help="Also append the Markdown report to the GitHub Actions job summary.")

    run = sub.add_parser(
        "run",
        help="Run a build command; on failure diagnose it and retry transient failures automatically.",
        description="Example: pipelinedoctor run --max-retries 2 -- mvn -B package",
    )
    run.add_argument("--max-retries", type=int, default=2, help="Retries for transient failures (default 2).")
    run.add_argument("--backoff", type=float, default=5.0, help="Seconds before the first retry; doubles each time.")
    run.add_argument("--log", default="pipelinedoctor-run.log", help="Where to save the command output.")
    run.add_argument("--shell", action="store_true", help="Run the command through the shell.")
    run.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    run.add_argument("--rules", help="Custom rules YAML.")
    run.add_argument("--kb", help="Custom knowledge base YAML.")
    run.add_argument("--no-llm", action="store_true", help="Never call the LLM.")
    run.add_argument("--github-summary", action="store_true", help="Append the run report to the GitHub job summary.")
    run.add_argument("cmd", nargs=argparse.REMAINDER, help="The command to run, after '--'.")

    lint = sub.add_parser("lint", help="Validate rules and knowledge-base YAML files (use as a CI gate).")
    lint.add_argument("--rules", help=f"Rules file to lint (default: bundled {DEFAULT_RULES.name}).")
    lint.add_argument("--kb", help=f"Knowledge base file to lint (default: bundled {DEFAULT_KB.name}).")
    lint.add_argument("--strict", action="store_true", help="Treat warnings as errors.")

    rules = sub.add_parser("rules", help="List the failure patterns PipelineDoctor knows.")
    rules.add_argument("--rules", help="Custom rules YAML.")
    return parser


def run_analyze(args: argparse.Namespace) -> int:
    doctor = Doctor(rules_file=args.rules, kb_file=args.kb)
    diagnoses = []
    for log in args.logs:
        try:
            diagnoses.append(doctor.diagnose(log, use_llm=not args.no_llm))
        except LogReadError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_INPUT_ERROR

    if args.format == "json":
        print(to_json(diagnoses))
    elif args.format == "junit":
        print(to_junit_xml(diagnoses))
    elif args.format == "markdown":
        print("\n".join(to_markdown(d) for d in diagnoses))
    else:
        print("\n\n".join(to_text(d) for d in diagnoses))

    if args.github_summary:
        write_github_summary("\n".join(to_markdown(d) for d in diagnoses))

    if args.fail_on_unknown and any(d.status == "unknown" for d in diagnoses):
        return EXIT_UNKNOWN
    return EXIT_OK


def write_github_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        print("warning: GITHUB_STEP_SUMMARY is not set (not running in GitHub Actions); summary skipped",
              file=sys.stderr)
        return
    with open(path, "a", encoding="utf-8") as summary:
        summary.write(markdown + "\n")


def run_run(args: argparse.Namespace) -> int:
    command = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not command:
        print("error: give a command after '--', e.g. pipelinedoctor run -- mvn -B package", file=sys.stderr)
        return EXIT_INPUT_ERROR
    doctor = Doctor(rules_file=args.rules, kb_file=args.kb)
    result = run_with_healing(
        command, doctor, Path(args.log), max_retries=max(0, args.max_retries),
        backoff=max(0.0, args.backoff), use_llm=not args.no_llm, shell=args.shell,
    )
    diagnosis_md = to_markdown(result.diagnosis) if result.diagnosis else ""
    report = run_summary_markdown(result, diagnosis_md)
    print()
    if args.format == "json":
        import json
        print(json.dumps({
            "command": result.command, "exit_code": result.exit_code, "healed": result.healed,
            "attempts": [a.__dict__ for a in result.attempts],
            "diagnosis": result.diagnosis.to_dict() if result.diagnosis else None,
        }, indent=2))
    elif args.format == "markdown":
        print(report)
    else:
        status = "RECOVERED" if result.healed else ("OK" if result.succeeded else "FAILED")
        print(f"[pipelinedoctor] {status} after {len(result.attempts)} attempt(s)")
        for a in result.attempts:
            print(f"  attempt {a.number}: exit {a.exit_code}, {a.outcome}{' - ' + a.detail if a.detail else ''}")
        if result.diagnosis and not result.succeeded:
            print()
            print(to_text(result.diagnosis))
    if args.github_summary:
        write_github_summary(report)
    # Pass the command's own exit code through so the pipeline still fails when it should.
    return result.exit_code


def run_lint(args: argparse.Namespace) -> int:
    rules_file = args.rules or str(DEFAULT_RULES)
    kb_file = args.kb or str(DEFAULT_KB)
    issues = lint_rules(rules_file) + lint_incidents(kb_file)
    for issue in issues:
        print(issue)
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    print(f"\nLint finished: {len(errors)} error(s), {len(warnings)} warning(s) in {rules_file}, {kb_file}")
    if errors or (args.strict and warnings):
        return EXIT_LINT_FAILED
    return EXIT_OK


def run_rules(args: argparse.Namespace) -> int:
    for rule in load_rules(args.rules):
        print(f"{rule.id:<30} {rule.severity:<7} {rule.category}")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "analyze":
            return run_analyze(args)
        if args.command == "lint":
            return run_lint(args)
        if args.command == "run":
            return run_run(args)
        return run_rules(args)
    except RulesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR


if __name__ == "__main__":
    sys.exit(main())
