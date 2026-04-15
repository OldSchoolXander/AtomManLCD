"""
cpu.py — CPU metrics: model name, usage, temperature, frequency, and fan speed.

All values are cached to avoid unnecessary sysfs reads on every tile cycle.
Cache TTLs:
  - cpu_usage_pct : 0.5 s  (sampled from /proc/stat delta)
  - cpu_temp_c    : 5 s    (read from coretemp hwmon)
  - cpu_freq_khz  : no TTL (instantaneous max across all cores)
  - fan_cpu_rpm   : 2 s    (CPU hwmon fan only, GPU fans excluded)
"""
import os
import time
import re
import glob

from utils import _run, _read


# ---------------------------------------------------------------------------
# CPU model name (read once, cached forever)
# ---------------------------------------------------------------------------

_cpu_model_cache: str | None = None


def cpu_model() -> str:
    """Return the CPU model name from /proc/cpuinfo (cached)."""
    global _cpu_model_cache
    if _cpu_model_cache is not None:
        return _cpu_model_cache
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            _cpu_model_cache = line.split(":", 1)[1].strip()
            return _cpu_model_cache
    _cpu_model_cache = "Linux CPU"
    return _cpu_model_cache


# ---------------------------------------------------------------------------
# CPU usage (delta-based, cached 0.5 s)
# ---------------------------------------------------------------------------

_cpu_usage_cache = {"usage": 0, "idle": 0, "total": 0, "ts": 0.0}


def cpu_usage_pct() -> int:
    """Return overall CPU usage percentage (0–100), refreshed at most every 0.5 s."""
    now = time.time()
    if now - _cpu_usage_cache["ts"] < 0.5:
        return _cpu_usage_cache["usage"]

    parts = _read("/proc/stat").splitlines()[0].split()[1:]
    n     = list(map(int, parts))
    idle  = n[3] + n[4]
    total = sum(n)

    di = idle  - _cpu_usage_cache["idle"]
    dt = total - _cpu_usage_cache["total"]

    if dt > 0 and _cpu_usage_cache["ts"] > 0:
        usage = max(0, min(100, int(round(100 * (1 - di / float(dt))))))
    else:
        usage = 0

    _cpu_usage_cache.update({"idle": idle, "total": total, "usage": usage, "ts": now})
    return usage


# ---------------------------------------------------------------------------
# CPU temperature (coretemp, cached 5 s)
# ---------------------------------------------------------------------------

_cpu_temp_cache = {"temp": 0, "hwmon_path": None, "ts": 0.0}


def cpu_temp_c() -> int:
    """Return CPU package temperature in °C (cached 5 s).

    Priority:
      1. coretemp / k10temp / zenpower hwmon with a Package/Core label.
      2. First temp1_input from any coretemp hwmon.
      3. First available temp from any hwmon (last resort).
    """
    now = time.time()
    if _cpu_temp_cache["ts"] > 0 and (now - _cpu_temp_cache["ts"]) < 5.0:
        return _cpu_temp_cache["temp"]

    def _store(v: int, hw: str) -> int:
        _cpu_temp_cache.update({"temp": v, "hwmon_path": hw, "ts": now})
        return v

    def _parse_temp(raw: str) -> int | None:
        if raw.isdigit():
            v = int(raw)
            return v // 1000 if v > 1000 else v
        return None

    # Pass 1: known CPU driver with a meaningful label
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        name = _read(os.path.join(hw, "name")).strip().lower()
        if name not in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            continue
        for n in range(8):
            label = _read(os.path.join(hw, f"temp{n}_label")).strip().lower()
            if any(k in label for k in ("package", "core", "tctl", "tdie")):
                v = _parse_temp(_read(os.path.join(hw, f"temp{n}_input")).strip())
                if v is not None:
                    return _store(v, hw)

    # Pass 2: first temp1_input from coretemp
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(os.path.join(hw, "name")).strip().lower() == "coretemp":
            v = _parse_temp(_read(os.path.join(hw, "temp1_input")).strip())
            if v is not None:
                return _store(v, hw)

    # Pass 3: any hwmon temp
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        for n in range(8):
            v = _parse_temp(_read(os.path.join(hw, f"temp{n}_input")).strip())
            if v is not None:
                return _store(v, hw)

    _cpu_temp_cache.update({"temp": 0, "ts": now})
    return 0


# ---------------------------------------------------------------------------
# CPU frequency (instantaneous max across all cores, no root required)
# ---------------------------------------------------------------------------

def cpu_freq_khz() -> int:
    """Return the highest current frequency across all CPU cores in kHz.

    Sources tried in order:
      1. scaling_cur_freq / cpuinfo_cur_freq via sysfs (all cores).
      2. /proc/cpuinfo  cpu MHz fields.
      3. lscpu  CPU MHz field (last resort).
    """
    max_freq = 0
    for cpu_dir in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"):
        for suffix in ("scaling_cur_freq", "cpuinfo_cur_freq"):
            s = _read(os.path.join(cpu_dir, suffix)).strip()
            if s.isdigit():
                max_freq = max(max_freq, int(s))
                break
    if max_freq > 0:
        return max_freq

    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("cpu MHz"):
            try:
                max_freq = max(max_freq, int(float(line.split(":", 1)[1].strip()) * 1000))
            except (ValueError, IndexError):
                pass
    if max_freq > 0:
        return max_freq

    out = _run(["lscpu"])
    m = re.search(r"CPU MHz:\s*([\d.]+)", out)
    return int(float(m.group(1)) * 1000) if m else 0


# ---------------------------------------------------------------------------
# CPU fan speed (hwmon, cached 2 s — GPU fans excluded)
# ---------------------------------------------------------------------------

class _FanCache:
    """Reads CPU fan RPM from hwmon, skipping GPU and NVMe hwmon devices."""

    _GPU_HWMON_NAMES = frozenset(("amdgpu", "nvidia", "nvme"))

    def __init__(self, cache_ttl: float = 2.0):
        self._ttl          = cache_ttl
        self._hwmon_path   : str | None = None
        self._cached_rpm   : int | None = None
        self._cached_time  : float      = 0.0

    def _discover(self) -> str:
        """Find the first hwmon fan path that belongs to a non-GPU device."""
        for hm in glob.glob("/sys/class/hwmon/hwmon*"):
            name = _read(os.path.join(hm, "name")).strip().lower()
            if name in self._GPU_HWMON_NAMES:
                continue
            for fan in glob.glob(os.path.join(hm, "fan*_input")):
                s = _read(fan).strip()
                if s.isdigit() and 0 < int(s) < 50_000:
                    return fan
        return ""

    def get_rpm(self) -> int:
        """Return CPU fan RPM (cached). Returns -1 when no sensor is found."""
        now = time.time()
        if self._cached_rpm is not None and (now - self._cached_time) < self._ttl:
            return self._cached_rpm

        if self._hwmon_path is None:
            self._hwmon_path = self._discover()

        if not self._hwmon_path:
            self._cached_rpm  = -1
            self._cached_time = now
            return -1

        s = _read(self._hwmon_path).strip()
        rpm = int(s) if s.isdigit() and int(s) > 0 else -1

        self._cached_rpm  = rpm
        self._cached_time = now
        return rpm


_fan_cache = _FanCache(cache_ttl=2.0)


def fan_cpu_rpm() -> int:
    """Return the CPU fan speed in RPM from hwmon (-1 if unavailable)."""
    return _fan_cache.get_rpm()
