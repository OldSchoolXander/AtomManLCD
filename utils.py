"""
utils.py — Shared low-level helpers used by all modules.

_run()  : run a subprocess and return stdout as a string (empty on error).
_read() : read a file and return its content as a string (empty on error).
"""
import subprocess


def _run(cmd: list, timeout: float = 0.7) -> str:
    """Run a command and return stdout. Returns '' on any error or timeout."""
    try:
        return subprocess.check_output(
            cmd, text=True, stderr=subprocess.DEVNULL, timeout=timeout
        )
    except Exception:
        return ""


def _read(path: str) -> str:
    """Read a file and return its content. Returns '' if the file is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""
