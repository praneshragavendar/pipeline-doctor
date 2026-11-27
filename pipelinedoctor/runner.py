"""Self-healing runner: run a build command, diagnose a failure, retry it if it is transient.

    pipelinedoctor run --max-retries 2 -- mvn -B package

Safety rule: only failures matched by a rule marked `retryable: true` in rules.yaml
(timeouts, network blips) are retried automatically. Everything else - including any
diagnosis that came from an LLM - is reported for a human to fix. A model guess never
triggers an action.
"""

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from pipelinedoctor.agents import Doctor
from pipelinedoctor.models import Diagnosis


@dataclass
class Attempt:
    number: int
    exit_code: int
    outcome: str  # "succeeded", "retried", "stopped"
    detail: str = ""
    rule_id: Optional[str] = None


@dataclass
class RunResult:
    command: str
    attempts: List[Attempt] = field(default_factory=list)
    diagnosis: Optional[Diagnosis] = None

    @property
    def exit_code(self) -> int:
        return self.attempts[-1].exit_code if self.attempts else 1

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0

    @property
    def healed(self) -> bool:
        return self.succeeded and len(self.attempts) > 1


def run_once(command: List[str], log_path: Path, shell: bool = False) -> int:
    """Run the command, streaming its output to the console and to the log file."""
    target = subprocess.list2cmdline(command) if shell and os.name == "nt" else (" ".join(command) if shell else command)
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen(
                target, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            message = f"pipelinedoctor: could not start command: {exc}\n"
            sys.stdout.write(message)
            log.write(message)
            return 127
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            log.write(line)
        return process.wait()


def run_with_healing(
    command: List[str],
    doctor: Doctor,
    log_path: Path,
    max_retries: int = 2,
    backoff: float = 5.0,
    use_llm: bool = True,
    shell: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    result = RunResult(command=" ".join(command))
    rules = {rule.id: rule for rule in doctor.rules}
    for number in range(1, max_retries + 2):
        print(f"\n[pipelinedoctor] attempt {number}: {result.command}", flush=True)
        code = run_once(command, log_path, shell=shell)
        if code == 0:
            result.attempts.append(Attempt(number, 0, "succeeded"))
            return result

        diagnosis = doctor.diagnose(str(log_path), use_llm=use_llm)
        result.diagnosis = diagnosis
        rule = rules.get(diagnosis.rule_id) if diagnosis.source == "rules" else None
        if rule and rule.retryable and number <= max_retries:
            wait = backoff * (2 ** (number - 1))
            detail = f"{rule.id} is a transient failure; retrying in {wait:g}s"
            print(f"[pipelinedoctor] {detail}", flush=True)
            result.attempts.append(Attempt(number, code, "retried", detail, rule.id))
            sleep(wait)
            continue

        if rule and rule.retryable:
            detail = f"{rule.id} is transient but it failed {number} times; giving up"
        elif diagnosis.status == "diagnosed":
            detail = f"{diagnosis.rule_id or diagnosis.category} is not safe to retry; needs a human (see the fix hints)"
        else:
            detail = "unknown failure; not retried"
        print(f"[pipelinedoctor] {detail}", flush=True)
        result.attempts.append(Attempt(number, code, "stopped", detail, diagnosis.rule_id))
        return result
    return result


def run_summary_markdown(result: RunResult, diagnosis_markdown: str = "") -> str:
    if result.healed:
        title = f"recovered automatically after {len(result.attempts) - 1} retry(s)"
    elif result.succeeded:
        title = "succeeded"
    else:
        title = f"failed (exit code {result.exit_code})"
    rows = "\n".join(
        f"| {a.number} | {a.exit_code} | {a.outcome} | {a.detail or '-'} |" for a in result.attempts
    )
    return (
        f"## PipelineDoctor run: {title}\n\n`{result.command}`\n\n"
        f"| Attempt | Exit code | Outcome | Detail |\n| --- | --- | --- | --- |\n{rows}\n\n"
        + (f"### Diagnosis of the failure\n\n{diagnosis_markdown}" if diagnosis_markdown else "")
    )
