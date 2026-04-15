"""
disk.py — Disk metrics: space usage, temperature, and drive label.

Public API:
  disk_numbers()  — (used_gb, total_gb, usage_pct) for the physical disk.
  disk_temp_c()   — drive temperature in °C (cached 5 s).
  disk_label()    — model/vendor string (e.g. "KINGSTON OM8PGP4…"), cached 1 h.
"""
import os
import re
import glob
import time

from utils import _run, _read


# ---------------------------------------------------------------------------
# Disk space — physical disk (mirrors Windows driver behaviour)
# ---------------------------------------------------------------------------

# Filesystem types that are virtual/pseudo and should never be summed.
_PSEUDO_FS = frozenset({
    "tmpfs", "devtmpfs", "sysfs", "proc", "cgroup", "cgroup2",
    "pstore", "efivarfs", "bpf", "tracefs", "debugfs", "securityfs",
    "fusectl", "hugetlbfs", "mqueue", "devpts", "overlay", "squashfs",
    "ramfs", "autofs", "binfmt_misc", "rpc_pipefs", "nfsd",
})


def _get_root_partition() -> str:
    """Return the block device name backing / (e.g. 'nvme0n1p2')."""
    src = _run(["findmnt", "-nro", "SOURCE", "/"]).strip()
    if src.startswith("/dev/"):
        return os.path.basename(src)
    # Fallback: parse /proc/mounts
    for line in _read("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "/" and parts[0].startswith("/dev/"):
            return os.path.basename(parts[0])
    return ""


def _partition_to_disk(part_name: str) -> str:
    """Given a partition name, return the parent physical disk name.

    Uses sysfs realpath: /sys/class/block/nvme0n1p2 → .../nvme0n1/nvme0n1p2
    so the parent directory basename is the disk name.
    Falls back to stripping the partition suffix.
    """
    link = f"/sys/class/block/{part_name}"
    if os.path.exists(link):
        real   = os.path.realpath(link)
        parent = os.path.basename(os.path.dirname(real))
        if os.path.exists(f"/sys/block/{parent}"):
            return parent
    # Fallback suffix stripping: nvme0n1p2 → nvme0n1, sda3 → sda
    name = re.sub(r'p\d+$', '', part_name)   # NVMe / MMC
    if name != part_name:
        return name
    return re.sub(r'\d+$', '', part_name)    # SATA / SCSI


def _disk_total_bytes(disk_name: str) -> int:
    """Return physical disk capacity in bytes from sysfs (no root needed)."""
    s = _read(f"/sys/class/block/{disk_name}/size").strip()
    return int(s) * 512 if s.isdigit() else 0


def _sum_used_bytes(disk_name: str) -> int:
    """Sum used bytes across all mounted partitions of disk_name.

    Reads /proc/mounts, skips pseudo-filesystems, deduplicates by device.
    """
    used = 0
    seen = set()
    for line in _read("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mountpoint, fstype = parts[0], parts[1], parts[2]
        if fstype in _PSEUDO_FS or not dev.startswith("/dev/"):
            continue
        dev_name = os.path.basename(dev)
        if dev_name in seen:
            continue
        # Accept partitions of our disk, or the disk itself mounted directly
        parent = _partition_to_disk(dev_name)
        if parent != disk_name and dev_name != disk_name:
            continue
        seen.add(dev_name)
        try:
            st   = os.statvfs(mountpoint)
            used += st.f_frsize * (st.f_blocks - st.f_bavail)
        except Exception:
            pass
    return used


def disk_numbers() -> tuple[int, int, int]:
    """Return (used_gb, total_gb, usage_pct) for the physical disk.

    Total  : physical disk capacity read from sysfs (entire drive).
    Used   : sum of used space across all mounted partitions of that drive.
    Mirrors what the Windows driver reports.
    Falls back to root-partition-only if disk detection fails.
    """
    part_name   = _get_root_partition()
    disk_name   = _partition_to_disk(part_name) if part_name else ""
    total_bytes = _disk_total_bytes(disk_name)  if disk_name else 0
    used_bytes  = _sum_used_bytes(disk_name)    if disk_name else 0

    if not total_bytes:                         # detection failed — root only
        st          = os.statvfs("/")
        total_bytes = st.f_frsize * st.f_blocks
        used_bytes  = st.f_frsize * (st.f_blocks - st.f_bavail)

    to_gb = lambda b: int(round(b / 1024 ** 3))
    usage = int(round(100.0 * used_bytes / float(total_bytes or 1)))
    return to_gb(used_bytes), to_gb(total_bytes), usage


# ---------------------------------------------------------------------------
# Disk temperature (hwmon, cached 5 s)
# ---------------------------------------------------------------------------

_disk_temp_cache = {"temp": 0, "ts": 0.0}
_DISK_TEMP_TTL   = 5.0
_DISK_TEMP_DEFAULT = 35   # fallback when no sensor is found


def disk_temp_c() -> int:
    """Return the primary drive temperature in °C (cached 5 s).

    Sources tried in order:
      1. nvme hwmon    — most reliable for NVMe drives.
      2. drivetemp hwmon — kernel 5.6+ for SATA drives.
      3. Default 35 °C  — when no sensor is available.
    """
    now = time.time()
    if _disk_temp_cache["ts"] > 0 and (now - _disk_temp_cache["ts"]) < _DISK_TEMP_TTL:
        return _disk_temp_cache["temp"]

    def _store(v: int) -> int:
        _disk_temp_cache.update({"temp": v, "ts": now})
        return v

    def _parse(raw: str) -> int | None:
        if raw.isdigit():
            v = int(raw)
            return v // 1000 if v > 1000 else v
        return None

    # NVMe hwmon
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(os.path.join(hwmon, "name")).strip() == "nvme":
            for n in range(4):
                v = _parse(_read(os.path.join(hwmon, f"temp{n}_input")).strip())
                if v is not None:
                    return _store(v)

    # SATA via drivetemp
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(os.path.join(hwmon, "name")).strip() == "drivetemp":
            v = _parse(_read(os.path.join(hwmon, "temp1_input")).strip())
            if v is not None:
                return _store(v)

    return _store(_DISK_TEMP_DEFAULT)


# ---------------------------------------------------------------------------
# Disk label / model name (cached 1 h)
# ---------------------------------------------------------------------------

_disk_label_cache: tuple[str, float] = ("", 0.0)
_DISK_LABEL_TTL = 3600.0


def disk_label() -> str:
    """Return the primary drive model string (cached 1 h).

    Sources tried in order:
      1. /sys/class/nvme/nvme*/model  (NVMe drives, no root needed).
      2. lsblk -dno NAME,MODEL,VENDOR  matched against the root device.
    """
    global _disk_label_cache
    label, ts = _disk_label_cache
    if label and (time.time() - ts) < _DISK_LABEL_TTL:
        return label

    label = _nvme_model() or _lsblk_model()
    label = re.sub(r"\s+", " ", label).strip()

    _disk_label_cache = (label, time.time())
    return label


def _nvme_model() -> str:
    """Read NVMe model from /sys/class/nvme/nvme*/model."""
    for nvme in sorted(glob.glob("/sys/class/nvme/nvme*")):
        model = _read(os.path.join(nvme, "model")).strip()
        if model:
            return model
    return ""


def _lsblk_model() -> str:
    """Fall back to lsblk to find the root device model."""
    out = _run(["lsblk", "-dno", "NAME,MODEL,VENDOR"])
    if not out:
        return ""

    # Try to identify the root device name
    root_dev = ""
    src = _run(["findmnt", "-nro", "SOURCE", "/"]).strip()
    if src:
        root_dev = os.path.basename(re.sub(r"p?\d+$", "", src.replace("/dev/", "")))

    pick = None
    for line in out.splitlines():
        parts = line.split(None, 2)
        if not parts:
            continue
        name = parts[0]
        rest = " ".join(parts[1:]) if len(parts) > 1 else ""
        if root_dev and name == root_dev:
            return rest
        if not root_dev and pick is None:
            pick = rest

    return pick or ""
