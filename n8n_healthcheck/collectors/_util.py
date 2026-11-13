"""Helpers shared by the collectors."""

import re
import subprocess
import sys

TIMEOUT = 5  # seconds, for every network call and subprocess


def warn(source: str, error: object) -> None:
    """One stderr line for a source that could not be collected. Never prints URL credentials."""
    message = re.sub(r"://[^@/\s]*@", "://***@", str(error))
    print(f"{source}: not collected ({message})", file=sys.stderr)


def run(args: list, env: dict | None = None, timeout: int = TIMEOUT) -> str:
    """Run a command and return its stdout; raise RuntimeError with a short message otherwise."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise RuntimeError(f"{args[0]} not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{args[0]} timed out after {timeout}s") from None
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        raise RuntimeError(lines[0] if lines else f"{args[0]} exited with {proc.returncode}")
    return proc.stdout
