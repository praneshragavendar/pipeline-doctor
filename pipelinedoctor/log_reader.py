"""Reading a build log and pulling out the lines that matter."""

import re
from pathlib import Path
from typing import List

# Words that usually appear on lines describing a failure.
ERROR_HINTS = re.compile(
    r"(error|fail|failed|failure|exception|fatal|denied|refused|"
    r"not found|cannot|could not|unable|timed out|killed|conflict)",
    re.IGNORECASE,
)

# ANSI colour codes that CI tools add to logs (e.g. "\x1b[31m").
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class LogReadError(Exception):
    """Raised when the log file cannot be read."""


def read_log(path: str) -> str:
    """Read a log file as text, whatever OS or encoding it came from."""
    log_path = Path(path)
    if not log_path.is_file():
        raise LogReadError(
            f"Log file not found: '{path}'. "
            "Fix hint: check the path, or pass the file with --log <path>."
        )
    try:
        raw = log_path.read_bytes()
    except OSError as exc:
        raise LogReadError(f"Could not read '{path}': {exc}") from exc

    # Logs from Windows agents are sometimes UTF-16 or cp1252; never crash on decoding.
    for encoding in ("utf-8", "utf-16", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    # Normalise Windows line endings and strip colour codes.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return ANSI_ESCAPE.sub("", text)


def error_lines(text: str, limit: int = 40) -> List[str]:
    """Return the lines that look like errors (first `limit` of them)."""
    lines = [line.strip() for line in text.splitlines()]
    return [line for line in lines if line and ERROR_HINTS.search(line)][:limit]


def tail(text: str, count: int = 30) -> List[str]:
    """Return the last `count` non-empty lines (failures are usually at the end)."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-count:]
