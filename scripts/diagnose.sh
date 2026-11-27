#!/usr/bin/env bash
# Diagnose one or more build logs. Works on Linux, macOS and WSL2.
# Usage: ./scripts/diagnose.sh build.log [more.log ...]
# Optional environment variables:
#   PIPELINEDOCTOR_FORMAT   text | json | markdown | junit   (default: text)
#   PIPELINEDOCTOR_PYTHON   python interpreter to use         (default: python3)
#   PIPELINEDOCTOR_RULES    custom rules file
#   ANTHROPIC_API_KEY       enables the LLM fallback
set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "usage: $0 <log-file> [more log files...]" >&2
    echo "fix hint: pass the path of a failed build log, e.g. $0 build.log" >&2
    exit 3
fi

FORMAT="${PIPELINEDOCTOR_FORMAT:-text}"
PYTHON="${PIPELINEDOCTOR_PYTHON:-python3}"

if command -v pipelinedoctor >/dev/null 2>&1; then
    exec pipelinedoctor analyze "$@" --format "$FORMAT"
fi

# Fall back to running from source (no install needed).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${SCRIPT_DIR}/..${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m pipelinedoctor analyze "$@" --format "$FORMAT"
