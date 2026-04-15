"""
network.py — Network interface detection and bandwidth metering.

Interface selection priority:
  1. Wired (eth*/enp*/eno*/ens*) with carrier, via default route.
  2. Any wired interface that is UP.
  3. Wireless (wlan0, etc.) as a last resort.

Virtual / container interfaces (lo, veth*, docker*, br-*, virbr*, tun*, tap*)
are always excluded.

Public API:
  fmt_rate(rate_kbs)   — format a K/s value as a human-readable string.
  NetMeter             — class: call .rates_ks() to get (rx_kbs, tx_kbs).
"""
import os
import re
import subprocess
import time

from utils import _read


# ---------------------------------------------------------------------------
# Formatting helper
# ---------------------------------------------------------------------------

def fmt_rate(rate_kbs: float | None) -> str:
    """Format a bandwidth rate (kB/s) as a compact human-readable string."""
    if rate_kbs is None:
        return "N/A"
    if rate_kbs >= 1_000_000:
        return f"{rate_kbs / 1_000_000:.2f}G/s"
    if rate_kbs >= 1_000:
        return f"{rate_kbs / 1_000:.2f}M/s"
    return f"{rate_kbs:.2f}K/s"


# ---------------------------------------------------------------------------
# Interface helpers
# ---------------------------------------------------------------------------

_VIRTUAL_PREFIXES = ("lo", "veth", "docker", "br-", "virbr", "tun", "tap", "dummy")


def _sh(cmd: list, timeout: float = 0.6) -> str:
    try:
        return subprocess.check_output(
            cmd, text=True, stderr=subprocess.DEVNULL, timeout=timeout
        ).strip()
    except Exception:
        return ""


def _is_wireless(iface: str) -> bool:
    return os.path.isdir(f"/sys/class/net/{iface}/wireless")


def _iface_info(iface: str) -> dict:
    info = {"name": iface, "up": False, "carrier": False, "wireless": _is_wireless(iface)}

    try:
        info["up"] = _read(f"/sys/class/net/{iface}/operstate").strip() == "up"
    except Exception:
        pass

    carrier_path = f"/sys/class/net/{iface}/carrier"
    if os.path.exists(carrier_path):
        try:
            info["carrier"] = int(_read(carrier_path).strip()) == 1
        except Exception:
            pass

    return info


def _default_route_ifaces() -> list[str]:
    """Return interface names that appear on the default route (order preserved)."""
    ifaces = []
    for line in _sh(["ip", "-o", "route", "show", "default"]).splitlines():
        parts = line.split()
        for i, p in enumerate(parts):
            if p == "dev" and i + 1 < len(parts):
                ifaces.append(parts[i + 1])
    return ifaces


def _list_candidate_ifaces() -> list[str]:
    """Return all non-virtual, non-loopback interfaces found in /sys/class/net."""
    try:
        return [
            i for i in sorted(os.listdir("/sys/class/net"))
            if not any(i.startswith(p) for p in _VIRTUAL_PREFIXES)
        ]
    except Exception:
        return []


def _pick_iface(preferred: str | None = None) -> str | None:
    """Select the best available network interface.

    Priority: preferred (if up+carrier) > wired default route > wired by name
              pattern > any UP interface.
    """
    # Honour an explicit override if it is actually up
    if preferred and os.path.isdir(f"/sys/class/net/{preferred}"):
        inf = _iface_info(preferred)
        if inf["up"] and inf["carrier"]:
            return preferred

    # Wired interface on the default route
    for iface in _default_route_ifaces():
        if not _is_wireless(iface):
            inf = _iface_info(iface)
            if inf["up"] and inf["carrier"]:
                return iface

    # Wired by common name patterns
    wired_prefixes = ("eth", "enp", "eno", "ens")
    ranked = []
    for iface in _list_candidate_ifaces():
        if iface.startswith(wired_prefixes) and not _is_wireless(iface):
            inf = _iface_info(iface)
            score = 10 if inf["up"] else (5 if inf["carrier"] else 0)
            if score > 0:
                ranked.append((score, iface))
    if ranked:
        return max(ranked)[1]

    # Any UP interface (including wireless)
    for iface in _list_candidate_ifaces():
        inf = _iface_info(iface)
        if inf["up"] or inf["carrier"]:
            return iface

    pool = _list_candidate_ifaces()
    return pool[0] if pool else None


# ---------------------------------------------------------------------------
# /proc/net/dev parsing
# ---------------------------------------------------------------------------

def _read_netdev() -> list[str]:
    try:
        with open("/proc/net/dev") as f:
            return f.read().splitlines()
    except Exception:
        return []


def _parse_netdev(lines: list[str], iface: str) -> tuple[int | None, int | None]:
    """Return (rx_bytes, tx_bytes) for the given interface, or (None, None)."""
    for line in lines:
        if iface not in line:
            continue
        parts = re.sub(r"\s+", " ", line).split(":")
        if len(parts) < 2:
            continue
        nums = parts[1].split()
        if len(nums) >= 10:
            return int(nums[0]), int(nums[8])
    return None, None


# ---------------------------------------------------------------------------
# NetMeter — tracks delta bytes and computes rates
# ---------------------------------------------------------------------------

class NetMeter:
    """Measures network RX/TX rates by sampling /proc/net/dev."""

    def __init__(self):
        preferred = os.getenv("ATOMMAN_NET_IFACE", "").strip() or None
        self.iface = _pick_iface(preferred)
        self.rx0 = self.tx0 = None
        self.t0: float | None = None
        self._prime()

    def _prime(self):
        """Record the initial byte counters for delta calculation."""
        if not self.iface:
            return
        lines = _read_netdev()
        rx, tx = _parse_netdev(lines, self.iface)
        if rx is None:
            self.iface = _pick_iface()
            if not self.iface:
                return
            lines = _read_netdev()
            rx, tx = _parse_netdev(lines, self.iface)
        if rx is not None:
            self.rx0, self.tx0, self.t0 = rx, tx, time.time()

    def _repick_if_needed(self):
        """Switch to a different interface if the current one went down."""
        if not self.iface:
            self.iface = _pick_iface()
            self._prime()
            return
        inf = _iface_info(self.iface)
        if not inf["up"] or (inf["wireless"] and not inf["carrier"]):
            new = _pick_iface()
            if new and new != self.iface:
                self.iface = new
                self._prime()

    def rates_ks(self) -> tuple[float | None, float | None]:
        """Return (rx_kbs, tx_kbs) since the last call, or (None, None) on error."""
        self._repick_if_needed()
        if not self.iface:
            return None, None

        lines = _read_netdev()
        rx1, tx1 = _parse_netdev(lines, self.iface)
        if rx1 is None or self.rx0 is None:
            self._prime()
            return None, None

        t1 = time.time()
        dt = max(1e-3, t1 - self.t0)
        rxk = max(0.0, (rx1 - self.rx0) / dt / 1024.0)
        txk = max(0.0, (tx1 - self.tx0) / dt / 1024.0)
        self.rx0, self.tx0, self.t0 = rx1, tx1, t1
        return rxk, txk
