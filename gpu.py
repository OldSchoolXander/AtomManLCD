"""
gpu.py — GPU detection and metrics.

Hardware context:
  - card0 : AMD Radeon RX 7900 XTX  (vendor 0x1002, hwmon = amdgpu)
  - card1 : Intel Arc Graphics       (vendor 0x8086, temp proxy = coretemp
                                      "Package id 0", driver = i915)

Detection order: AMD → Intel → NVIDIA (AMD is the primary display GPU).

Public API:
  clean_gpu_name(name)        — strip vendor noise from a GPU name string.
  detect_gpu_vendor()         — return "amd" | "intel" | "nvidia" | "unknown".
  gpu_info()                  — (name, temp_c, util_pct) for the primary GPU.
  gpu_vram_info()             — (vram_used_mb, vram_total_mb) for AMD GPU.
  get_intel_arc_info()        — (name, temp_c, util_pct) for Intel Arc.
  intel_igpu_mem_info()       — (used_mb, total_mb) for Intel Arc iGPU RAM.

Cache TTLs:
  gpu_info              : 10 s
  get_intel_arc_info    : name/temp 10 s, util 3 s (fdinfo engine-delta)
  intel_igpu_mem_info   : 3 s (fdinfo scan)
"""
import os
import re
import glob
import time

from utils import _run, _read


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_gpu_name(name: str) -> str:
    """Remove common vendor noise from a raw GPU name string."""
    s = name.strip()
    s = re.sub(
        r"\(R\)|\(TM\)|NVIDIA Corporation|Advanced Micro Devices,? Inc\.?|Intel\(R\)\s*",
        "", s, flags=re.I,
    )
    return re.sub(r"\s+", " ", s).strip() or "GPU"


# ---------------------------------------------------------------------------
# Vendor detection (cached forever — hardware doesn't change at runtime)
# ---------------------------------------------------------------------------

_gpu_vendor_cache: str | None = None

_VENDOR_IDS = {
    0x1002: "amd",
    0x8086: "intel",
    0x10DE: "nvidia",
}


def detect_gpu_vendor() -> str:
    """Detect the primary GPU vendor by reading PCI vendor IDs from sysfs.

    Returns one of: "amd", "intel", "nvidia", "unknown".
    AMD is checked first since it is the main display GPU on this system.
    """
    global _gpu_vendor_cache
    if _gpu_vendor_cache is not None:
        return _gpu_vendor_cache

    # Preferred order: AMD first, then Intel, then NVIDIA.
    for preferred_vendor in ("amd", "intel", "nvidia"):
        target_id = next(k for k, v in _VENDOR_IDS.items() if v == preferred_vendor)
        for vendor_path in glob.glob("/sys/class/drm/card*/device/vendor"):
            try:
                if int(_read(vendor_path).strip(), 16) == target_id:
                    _gpu_vendor_cache = preferred_vendor
                    return preferred_vendor
            except (ValueError, IOError):
                pass

    _gpu_vendor_cache = "unknown"
    return "unknown"


# ---------------------------------------------------------------------------
# Primary GPU info (AMD Radeon RX 7900 XTX)
# ---------------------------------------------------------------------------

_gpu_cache = {"name": "", "temp": 0, "util": 0, "vram_used_mb": 0, "vram_total_mb": 0, "ts": 0.0}


def gpu_info() -> tuple[str, int, int]:
    """Return (name, temp_c, util_pct) for the primary GPU (cached 10 s).

    AMD path  : reads hwmon edge/junction temperature, gpu_busy_percent and
                VRAM usage directly from sysfs.
    Intel path: uses coretemp "Package id 0" as the SoC die temperature.
    NVIDIA path: falls back to nvidia-smi (rarely used on this system).
    """
    now = time.time()
    if _gpu_cache["ts"] > 0 and (now - _gpu_cache["ts"]) < 10.0:
        return _gpu_cache["name"], _gpu_cache["temp"], _gpu_cache["util"]

    vendor = detect_gpu_vendor()
    name, temp, util = "GPU", 0, 0
    vram_used_mb, vram_total_mb = 0, 0

    if vendor == "amd":
        name, temp, util, vram_used_mb, vram_total_mb = _amd_gpu_info()
    elif vendor == "intel":
        name, temp, util = get_intel_arc_info()
    elif vendor == "nvidia":
        name, temp, util = _nvidia_gpu_info()

    _gpu_cache.update({
        "name": name, "temp": temp, "util": util,
        "vram_used_mb": vram_used_mb, "vram_total_mb": vram_total_mb,
        "ts": now,
    })
    return name, temp, util


def gpu_vram_info() -> tuple[int, int]:
    """Return (vram_used_mb, vram_total_mb) for the primary GPU.

    Forces a cache refresh if not yet populated.
    Only meaningful for AMD (Intel Arc has no dedicated VRAM).
    """
    if _gpu_cache["ts"] == 0.0:
        gpu_info()
    return _gpu_cache["vram_used_mb"], _gpu_cache["vram_total_mb"]


# ---------------------------------------------------------------------------
# AMD GPU — fdinfo-based VRAM metrics (cached 3 s)
# ---------------------------------------------------------------------------
# sysfs mem_info_vram_used does NOT account for Vulkan (radv) allocations.
# The amdgpu driver tracks per-client VRAM in /proc/<pid>/fdinfo/<fd>:
#   drm-driver:      amdgpu
#   drm-client-id:   <N>
#   drm-memory-vram: <X> KiB   ← VRAM allocated by this client (KiB)
#   drm-memory-gtt:  <X> KiB   ← GTT (system RAM mapped for GPU)
# Summing drm-memory-vram deduplicated by client-id gives the real total.

_amd_fdinfo_cache: dict = {"vram_used_mb": 0, "ts": 0.0}

_RE_AMDGPU_DRIVER = re.compile(r"^drm-driver:\s*amdgpu\b",   re.M)
_RE_AMD_CLIENT_ID = re.compile(r"^drm-client-id:\s*(\d+)",   re.M)
_RE_AMD_MEM_VRAM  = re.compile(r"^drm-memory-vram:\s*(\d+)", re.M)


def _scan_amd_fdinfo() -> dict[int, dict]:
    """Scan /proc/*/fdinfo/* for open amdgpu DRM file descriptors.

    Returns {client_id: {'mem_kib': int}}.
    Deduplication by drm-client-id prevents counting shared BOs twice.
    Requires root to read other processes' fdinfo.
    """
    clients: dict[int, dict] = {}
    try:
        proc_scan = os.scandir("/proc")
    except OSError:
        return clients

    for pid_entry in proc_scan:
        if not pid_entry.name.isdigit():
            continue
        try:
            fd_scan = os.scandir(os.path.join(pid_entry.path, "fdinfo"))
        except OSError:
            continue

        for fd_entry in fd_scan:
            try:
                with open(fd_entry.path, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except OSError:
                continue

            if not _RE_AMDGPU_DRIVER.search(content):
                continue

            m = _RE_AMD_CLIENT_ID.search(content)
            if not m:
                continue
            cid = int(m.group(1))

            mem_kib = 0
            m = _RE_AMD_MEM_VRAM.search(content)
            if m:
                mem_kib = int(m.group(1))   # value is in KiB

            # Multiple fds may share a client-id; take the maximum seen.
            if cid not in clients:
                clients[cid] = {"mem_kib": mem_kib}
            else:
                clients[cid]["mem_kib"] = max(clients[cid]["mem_kib"], mem_kib)

    return clients


def _refresh_amd_fdinfo() -> None:
    """Refresh the AMD GPU fdinfo VRAM cache. TTL = 3 s."""
    global _amd_fdinfo_cache

    now = time.time()
    if _amd_fdinfo_cache["ts"] > 0 and (now - _amd_fdinfo_cache["ts"]) < 3.0:
        return

    clients = _scan_amd_fdinfo()
    used_kib = sum(c["mem_kib"] for c in clients.values())
    _amd_fdinfo_cache.update({"vram_used_mb": used_kib // 1024, "ts": now})


def _amd_gpu_info() -> tuple[str, int, int, int, int]:
    """Read AMD Radeon info from sysfs + fdinfo (no rocm-smi needed).

    Returns (name, temp_c, util_pct, vram_used_mb, vram_total_mb).

    VRAM used : fdinfo drm-memory-vram (correctly tracks Vulkan/radv allocations).
    VRAM total: sysfs mem_info_vram_total (accurate, static hardware value).
    """
    temp = 0
    util = 0
    vram_used_mb = 0
    vram_total_mb = 0
    name = "AMD Radeon RX 7900 XTX"

    # Find the amdgpu hwmon directory
    hw_path = ""
    for p in glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*"):
        hw_name = _read(os.path.join(os.path.dirname(p), "name")).strip().lower()
        if "amdgpu" in hw_name:
            hw_path = p
            break
    if not hw_path:
        for hw in glob.glob("/sys/class/hwmon/hwmon*"):
            if _read(os.path.join(hw, "name")).strip().lower() == "amdgpu":
                hw_path = hw
                break

    # Read temperature: edge > junction > mem (first valid wins)
    if hw_path:
        for label_priority in ("edge", "junction", "mem"):
            for n in range(1, 10):
                label = _read(os.path.join(hw_path, f"temp{n}_label")).strip()
                if label == label_priority:
                    s = _read(os.path.join(hw_path, f"temp{n}_input")).strip()
                    if s.isdigit():
                        temp = int(s) // 1000 if int(s) > 1000 else int(s)
                        break
            if temp:
                break

    # GPU name from sysfs product_name or device name
    for path in ("/sys/class/drm/card0/device/product_name",
                 "/sys/class/drm/card0/device/name"):
        if os.path.exists(path):
            raw = clean_gpu_name(_read(path).strip())
            if raw:
                name = raw
            break

    # GPU utilization % (amdgpu driver exposes this directly)
    s = _read("/sys/class/drm/card0/device/gpu_busy_percent").strip()
    if s.isdigit():
        util = int(s)

    # VRAM used: fdinfo scan (correctly tracks Vulkan/radv BOs).
    # VRAM total: sysfs (bytes → MiB, static hardware value).
    _refresh_amd_fdinfo()
    vram_used_mb = _amd_fdinfo_cache["vram_used_mb"]
    s = _read("/sys/class/drm/card0/device/mem_info_vram_total").strip()
    if s.isdigit():
        vram_total_mb = int(s) // (1024 * 1024)

    return name, temp, util, vram_used_mb, vram_total_mb


def _nvidia_gpu_info() -> tuple[str, int, int]:
    """Read NVIDIA GPU info via nvidia-smi (fallback only)."""
    out = _run(
        ["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu",
         "--format=csv,noheader,nounits"],
        timeout=1.0,
    )
    if out:
        try:
            name_raw, temp_str, util_str = [x.strip() for x in out.splitlines()[0].split(",")]
            return clean_gpu_name(name_raw), int(temp_str), int(util_str)
        except Exception:
            pass
    return "NVIDIA GPU", 0, 0


# ---------------------------------------------------------------------------
# Intel Arc iGPU — fdinfo-based metrics (util + memory, cached 3 s)
# ---------------------------------------------------------------------------
# The i915 driver exposes per-client GPU stats via /proc/<pid>/fdinfo/<fd>:
#   drm-driver:        i915
#   drm-pdev:          0000:00:02.0
#   drm-client-id:     <N>          (unique DRM context id)
#   drm-engine-render: <X> ns       (cumulative render-engine busy time for this client)
#   drm-total-system0: <Y> KiB      (KiB of system RAM allocated for this client's GEM objects)
#
# GPU utilization % = delta(sum engine_ns) / delta(wall_ns)  (capped 0–100).
# Memory used        = sum(drm-total-system0) deduplicated by client-id.
# Memory total       = 50% of MemTotal (practical upper bound for iGPU allocation).

_intel_fdinfo_cache: dict = {"util": 0, "mem_used_mb": 0, "mem_total_mb": 0, "ts": 0.0}
_intel_engine_prev:  dict = {"sum_ns": 0, "ts": 0.0}

_RE_I915_DRIVER   = re.compile(r"^drm-driver:\s*i915\b",        re.M)
_RE_CLIENT_ID     = re.compile(r"^drm-client-id:\s*(\d+)",      re.M)
_RE_ENGINE_RENDER = re.compile(r"^drm-engine-render:\s*(\d+)",  re.M)
_RE_MEM_SYSTEM0   = re.compile(r"^drm-total-system0:\s*(\d+)",  re.M)


def _scan_intel_fdinfo() -> dict[int, dict]:
    """Scan /proc/*/fdinfo/* for open i915 (Intel Arc) DRM file descriptors.

    Returns {client_id: {'engine_ns': int, 'mem_kib': int}}.
    Deduplication by drm-client-id ensures shared objects aren't counted twice.
    Requires the process to run as root to read other processes' fdinfo.
    """
    clients: dict[int, dict] = {}
    try:
        proc_scan = os.scandir("/proc")
    except OSError:
        return clients

    for pid_entry in proc_scan:
        if not pid_entry.name.isdigit():
            continue
        try:
            fd_scan = os.scandir(os.path.join(pid_entry.path, "fdinfo"))
        except OSError:
            continue

        for fd_entry in fd_scan:
            try:
                with open(fd_entry.path, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except OSError:
                continue

            if not _RE_I915_DRIVER.search(content):
                continue

            m = _RE_CLIENT_ID.search(content)
            if not m:
                continue
            cid = int(m.group(1))

            engine_ns = 0
            m = _RE_ENGINE_RENDER.search(content)
            if m:
                engine_ns = int(m.group(1))

            mem_kib = 0
            m = _RE_MEM_SYSTEM0.search(content)
            if m:
                mem_kib = int(m.group(1))   # value is in KiB, not bytes

            # Multiple fds may share a client-id; take the maximum seen.
            if cid not in clients:
                clients[cid] = {"engine_ns": engine_ns, "mem_kib": mem_kib}
            else:
                clients[cid]["engine_ns"] = max(clients[cid]["engine_ns"], engine_ns)
                clients[cid]["mem_kib"]   = max(clients[cid]["mem_kib"],   mem_kib)

    return clients


def _refresh_intel_fdinfo() -> None:
    """Refresh the Intel iGPU fdinfo cache (util% + memory). TTL = 3 s."""
    global _intel_fdinfo_cache, _intel_engine_prev

    now = time.time()
    if _intel_fdinfo_cache["ts"] > 0 and (now - _intel_fdinfo_cache["ts"]) < 3.0:
        return

    clients = _scan_intel_fdinfo()

    # --- GPU utilization % (engine-time delta) ---
    sum_engine_ns = sum(c["engine_ns"] for c in clients.values())
    prev_sum = _intel_engine_prev["sum_ns"]
    prev_ts  = _intel_engine_prev["ts"]

    util = 0
    if prev_ts > 0 and sum_engine_ns >= prev_sum:
        elapsed_ns = (now - prev_ts) * 1e9
        if elapsed_ns > 0:
            util = int(min(100, max(0, (sum_engine_ns - prev_sum) / elapsed_ns * 100)))

    _intel_engine_prev["sum_ns"] = sum_engine_ns
    _intel_engine_prev["ts"]     = now

    # --- Memory: used (KiB→MB) and total (50% of MemTotal) ---
    used_kib = sum(c["mem_kib"] for c in clients.values())
    used_mb  = used_kib // 1024

    mem_total_mb = 0
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                mem_total_mb = int(parts[1]) // 1024 // 2   # kB → MB, then 50%
            break

    _intel_fdinfo_cache.update({
        "util":         util,
        "mem_used_mb":  used_mb,
        "mem_total_mb": mem_total_mb,
        "ts":           now,
    })


def intel_igpu_mem_info() -> tuple[int, int]:
    """Return (used_mb, total_mb) for the Intel Arc iGPU (cached 3 s).

    used_mb  = sum of drm-total-system0 across all unique i915 clients.
    total_mb = 50% of system RAM (practical upper bound for iGPU allocation).
    Returns (0, 0) when no i915 clients are found or running without root.
    """
    _refresh_intel_fdinfo()
    return _intel_fdinfo_cache["mem_used_mb"], _intel_fdinfo_cache["mem_total_mb"]


# ---------------------------------------------------------------------------
# Intel Arc temperature — sensor selection + EMA smoothing
# ---------------------------------------------------------------------------
# The Arc iGPU shares the Meteor Lake die with the CPU.  "Package id 0" in
# coretemp is the *maximum* temperature across all tiles (CPU P-cores,
# E-cores, SoC tile, …) so a brief CPU burst can spike it to 70 °C even
# when the GPU itself is cold.
#
# Sensor priority:
#   1. i915 native hwmon on card1  (GPU-junction temp, only available on
#      some kernel/firmware combos — gives a true GPU tile reading)
#   2. coretemp "Package id 0"     (SoC-wide proxy, always available)
#
# A lightweight EMA (α = 0.5) is applied on top so that single-sample CPU
# spikes are halved rather than shown raw on the display.

_intel_temp_ema: float = 0.0   # module-level EMA state


def _read_intel_gpu_temp() -> int:
    """Read the best available Intel Arc temperature in °C.

    Tries, in order:
      1. i915 hwmon on /sys/class/drm/card1/device/hwmon/hwmon*/temp1_input
         (direct GPU junction temperature when exposed by the driver)
      2. coretemp hwmon label "Package id 0"
         (SoC-wide proxy; also reflects CPU activity)
    Returns 0 when no sensor is accessible.
    """
    # 1. i915 native GPU hwmon on card1
    for hw in glob.glob("/sys/class/drm/card1/device/hwmon/hwmon*"):
        s = _read(os.path.join(hw, "temp1_input")).strip()
        if s.isdigit():
            v = int(s)
            if v > 0:
                return v // 1000 if v > 1000 else v

    # 2. coretemp "Package id 0" fallback
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(os.path.join(hw, "name")).strip().lower() != "coretemp":
            continue
        for n in range(1, 30):
            label_path = os.path.join(hw, f"temp{n}_label")
            if not os.path.exists(label_path):
                continue
            if "package id 0" in _read(label_path).strip().lower():
                s = _read(os.path.join(hw, f"temp{n}_input")).strip()
                if s.isdigit():
                    v = int(s)
                    return v // 1000 if v > 1000 else v
                return 0   # label found but value unreadable
    return 0


def _apply_intel_temp_ema(raw: int) -> int:
    """Apply an exponential moving average (α = 0.5) to the raw temperature.

    α = 0.5 means each new reading contributes 50 % of the displayed value,
    halving any single-sample spike while still tracking sustained changes
    within ~3–4 refresh cycles (≈ 30–40 s with the 10 s cache).

    The EMA is seeded on the first valid reading so there is no cold-start
    lag.  A raw value of 0 is ignored (sensor read failure) and the last
    smoothed value is returned unchanged.
    """
    global _intel_temp_ema
    if raw <= 0:
        return int(round(_intel_temp_ema)) if _intel_temp_ema > 0.0 else 0
    if _intel_temp_ema <= 0.0:
        _intel_temp_ema = float(raw)   # seed: no lag on first valid reading
    else:
        _intel_temp_ema = 0.5 * raw + 0.5 * _intel_temp_ema
    return int(round(_intel_temp_ema))


# ---------------------------------------------------------------------------
# Intel Arc info (always card1 on this system, cached 10 s)
# ---------------------------------------------------------------------------

_intel_cache = {"name": "Intel Arc Graphics", "temp": 0, "ts": 0.0}


def get_intel_arc_info() -> tuple[str, int, int]:
    """Return (name, temp_c, util_pct) for the Intel Arc GPU.

    Name + temperature: cached 10 s (sysfs / coretemp).
    Utilization      : cached  3 s (fdinfo engine-delta, always fresh).

    Temperature is read via _read_intel_gpu_temp() (i915 hwmon → coretemp
    fallback) and smoothed through an EMA to suppress CPU-driven spikes.
    Utilization is computed from drm-engine-render delta across all i915
    clients in /proc/*/fdinfo (requires root; returns 0 as non-root).
    """
    now = time.time()

    # Util is always refreshed via its own 3s fdinfo cache.
    _refresh_intel_fdinfo()
    intel_util = _intel_fdinfo_cache["util"]

    # Name + temp served from 10s cache.
    if _intel_cache["ts"] > 0 and (now - _intel_cache["ts"]) < 10.0:
        return _intel_cache["name"], _intel_cache["temp"], intel_util

    # Read raw temperature and apply EMA smoothing.
    raw_temp   = _read_intel_gpu_temp()
    intel_temp = _apply_intel_temp_ema(raw_temp)

    # GPU name from card1
    intel_name = "Intel Arc Graphics"
    for path in ("/sys/class/drm/card1/device/product_name",
                 "/sys/class/drm/card1/device/name"):
        if os.path.exists(path):
            raw = clean_gpu_name(_read(path).strip())
            if raw:
                intel_name = raw
            break
    if "Intel" not in intel_name and "Arc" not in intel_name:
        intel_name = "Intel Arc Graphics"

    _intel_cache.update({"name": intel_name, "temp": intel_temp, "ts": now})
    return intel_name, intel_temp, intel_util
