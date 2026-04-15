"""
colors.py — ANSI color codes and colorization helpers for the dashboard.

The global NOCOLOR flag is set by main() when --no-color is passed.
All other modules that need colors should import from here.
"""

# Set to True by main() when --no-color CLI flag is used.
NOCOLOR = False


class C:
    """ANSI escape codes for terminal colors."""
    R  = "\033[31m";  G  = "\033[32m";  Y  = "\033[33m"
    B  = "\033[34m";  M  = "\033[35m";  C_ = "\033[36m";  W  = "\033[37m"
    BR = "\033[91m";  BG = "\033[92m";  BY = "\033[93m"
    BB = "\033[94m";  BM = "\033[95m";  BC = "\033[96m";  BW = "\033[97m"
    DIM   = "\033[2m"
    RESET = "\033[0m"


def colorize(txt: str, color: str) -> str:
    """Wrap text in an ANSI color code (no-op when NOCOLOR is True)."""
    if NOCOLOR:
        return txt
    return f"{color}{txt}{C.RESET}"


def temp_color(t):
    """Return a colorizer function based on a temperature value (°C)."""
    try:
        t = float(t)
    except Exception:
        return lambda s: s
    if t < 60:
        return lambda s: colorize(s, C.BG)   # green  — cool
    if t < 80:
        return lambda s: colorize(s, C.BY)   # yellow — warm
    return     lambda s: colorize(s, C.BR)   # red    — hot


def util_color(pct):
    """Return a colorizer function based on a utilization percentage."""
    try:
        pct = float(pct)
    except Exception:
        return lambda s: s
    if pct < 40:
        return lambda s: colorize(s, C.BG)   # green  — low load
    if pct < 80:
        return lambda s: colorize(s, C.BY)   # yellow — moderate
    return     lambda s: colorize(s, C.BR)   # red    — high load


def usage_color(pct):
    """Return a colorizer function based on a disk/memory usage percentage."""
    try:
        pct = float(pct)
    except Exception:
        return lambda s: s
    if pct < 70:
        return lambda s: colorize(s, C.BG)   # green  — plenty of space
    if pct < 90:
        return lambda s: colorize(s, C.BY)   # yellow — getting full
    return     lambda s: colorize(s, C.BR)   # red    — nearly full
