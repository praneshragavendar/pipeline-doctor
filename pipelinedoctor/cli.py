"""Command-line interface.

Examples:
    pipelinedoctor analyze build.log
    pipelinedoctor analyze logs/*.log --format json
    pipelinedoctor analyze build.log --no-llm --fail-on-unknown
    pipelinedoctor analyze build.log --format junit > diagnosis.xml
    pipelinedoctor lint
    pipelinedoctor rules
"""

import argparse
import sys
from typing import List, Optional

from pipelinedoctor import __version__
from pipelinedoctor.agents import Doctor
from pipelinedoctor.log_reader import LogReadError
from pipelinedoctor.linter import lint_incidents, lint_rules
from pipelinedoctor.report import to_json, to_junit_xml, to_markdown, to_text
from pipelinedoctor.rules_engine import DEFAULT_RULES, RulesError, load_rules
from pipelinedoctor.knowledge import DEFAULT_KB

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

    if args.fail_on_unknown and any(d.status == "unknown" for d in diagnoses):
        return EXIT_UNKNOWN
    return EXIT_OK


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
        return run_rules(args)
    except RulesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR


if __name__ == "__main__":
    sys.exit(main())
