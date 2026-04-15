"""
ram.py — RAM metrics: usage and manufacturer label.

Public API:
  mem_info()   — (used_gb, avail_gb, total_gb, usage_pct)
  ram_label()  — manufacturer string (e.g. "Crucial"), cached 1 h.

RAM vendor detection — methods tried in order:
  1. dmidecode -t memory           (requires root, or NOPASSWD sudoers rule)
  2. sudo -n dmidecode -t memory   (requires NOPASSWD sudoers rule)
  3. /sys/firmware/dmi/entries/17-*/raw  (SMBIOS sysfs, needs root on most distros)
  4. lshw -class memory            (works on many distros without root)
  5. /dev/i2c-N via SPD5118 EEPROM (DDR5; needs user in 'i2c' group)

To enable vendor detection, run the script as root or grant passwordless dmidecode:
  echo 'YOUR_USER ALL=(ALL) NOPASSWD: /usr/bin/dmidecode' | sudo tee /etc/sudoers.d/atomman-dmi
Or add yourself to the i2c group (DDR5 systems):
  sudo usermod -a -G i2c YOUR_USER   (then log out and back in)
"""
import fcntl
import glob
import os
import re
import struct
import time

from utils import _run, _read


# ---------------------------------------------------------------------------
# RAM usage (read from /proc/meminfo, no caching needed)
# ---------------------------------------------------------------------------

def mem_info() -> tuple[float, float, float, int]:
    """Return (used_gb, avail_gb, total_gb, usage_pct) from /proc/meminfo."""
    data: dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        parts = line.replace(":", "").split()
        if len(parts) >= 2 and parts[1].isdigit():
            data[parts[0]] = int(parts[1])  # values are in kB

    total = data.get("MemTotal", 0)
    avail = data.get("MemAvailable", 0)
    used  = max(0, total - avail)

    to_gb = lambda kb: round(kb / 1024.0 / 1024.0, 1)
    usage = int(round(100.0 * used / float(total or 1)))

    return to_gb(used), to_gb(avail), to_gb(total), usage


# ---------------------------------------------------------------------------
# RAM manufacturer label (cached 1 h)
# ---------------------------------------------------------------------------

_ram_label_cache: tuple[str | None, float] = (None, 0.0)
_RAM_CACHE_TTL = 3600.0

_MANU_INVALID = frozenset({
    "Undefined", "Not Specified", "Unknown",
    "To Be Filled By O.E.M.", "",
})

_MANU_NORMALIZATIONS = {
    "Micron Technology": "Micron",
    "Samsung Electronics": "Samsung",
    "HYNIX": "SK Hynix",
    "Hynix Semiconductor": "SK Hynix",
    "Hynix": "SK Hynix",
}

# JEDEC JEP106 manufacturer lookup: (bank, raw_byte) → name
# bank = number of 0x7F continuation codes (0-indexed)
# raw_byte = 8-bit value including odd-parity MSB
_JEDEC_MANUFACTURERS: dict[tuple[int, int], str] = {
    # bank 0
    (0, 0x2C): "Micron",        # Crucial is Micron branded
    (0, 0xAD): "SK Hynix",
    (0, 0xCE): "Samsung",
    (0, 0x9E): "Kingston",
    (0, 0x04): "Fujitsu",
    (0, 0x98): "Toshiba",
    # bank 1
    (1, 0x98): "Kingston",
    (1, 0x9E): "Kingston",
    (1, 0x51): "ADATA",
    (1, 0xF1): "G.SKILL",
    (1, 0x43): "Ramaxel",
    (1, 0x04): "Transcend",
    # bank 2
    (2, 0x9E): "Kingston",
    (2, 0x51): "ADATA",
    # bank 4
    (4, 0x43): "Ramaxel",
    # bank 5
    (5, 0x01): "Kingston",
    (5, 0x51): "ADATA",
}


def ram_label() -> str:
    """Return the RAM manufacturer name (cached 1 h).

    Tries multiple sources in order until one succeeds.
    Returns an empty string when no manufacturer info is accessible.
    """
    global _ram_label_cache
    label, ts = _ram_label_cache
    if label is not None and (time.time() - ts) < _RAM_CACHE_TTL:
        return label

    manu = (
        _try_dmidecode()
        or _try_dmi_sysfs()
        or _try_lshw()
        or _try_spd_i2c()
    )
    manu = _normalize_manu(manu)

    _ram_label_cache = (manu, time.time())
    return manu


# ---------------------------------------------------------------------------
# Method 1: dmidecode (requires root or NOPASSWD sudoers)
# ---------------------------------------------------------------------------

def _try_dmidecode() -> str:
    """Try reading RAM manufacturer via dmidecode (requires root or NOPASSWD sudo)."""
    out = (
        _run(["dmidecode", "-t", "memory"])
        or _run(["sudo", "-n", "dmidecode", "-t", "memory"])
    )
    if not out:
        return ""
    return _parse_dmidecode_output(out)


def _parse_dmidecode_output(out: str) -> str:
    """Extract the first valid Manufacturer value from dmidecode output."""
    for m in re.finditer(r"^\s*Manufacturer:\s*(.+)$", out,
                          re.MULTILINE | re.IGNORECASE):
        value = m.group(1).strip()
        if value not in _MANU_INVALID:
            return value
    return ""


# ---------------------------------------------------------------------------
# Method 2: SMBIOS DMI sysfs (readable without root on some kernels)
# ---------------------------------------------------------------------------

def _try_dmi_sysfs() -> str:
    """Try reading RAM manufacturer from /sys/firmware/dmi/entries (no root needed
    on some kernels — on most distros this is root-only).

    Correctly follows SMBIOS spec:
      - Byte 1 of the structure header = length of fixed-length part
      - For SMBIOS 2.3+, byte 0x17 = Manufacturer string index (1-based)
    """
    for entry in glob.glob("/sys/firmware/dmi/entries/17-*/raw"):
        try:
            with open(entry, "rb") as f:
                raw = f.read()
            if len(raw) < 2:
                continue

            struct_len = raw[1]   # byte 1 = length of fixed part per SMBIOS spec
            # Manufacturer string index is at offset 0x17 (SMBIOS 2.3+, struct_len > 0x17)
            if struct_len <= 0x17 or len(raw) <= struct_len:
                continue

            mfr_idx = raw[0x17]   # 1-based index into string table (0 = absent)
            if mfr_idx == 0:
                continue

            # String table starts immediately after the fixed-length block,
            # entries separated by NUL, terminated by double NUL.
            strings = raw[struct_len:].split(b"\x00")
            if mfr_idx <= len(strings):
                value = strings[mfr_idx - 1].decode("ascii", errors="ignore").strip()
                if value and value not in _MANU_INVALID:
                    return value
        except Exception:
            pass
    return ""


# ---------------------------------------------------------------------------
# Method 3: lshw (multilingual)
# ---------------------------------------------------------------------------

def _try_lshw() -> str:
    """Try lshw -class memory. Works on many distros without root.

    Matches the manufacturer field in any language (EN/FR/DE/ES/IT).
    Skips entries that look like chipset/controller rather than DRAM DIMMs.
    Uses a 3-second timeout because lshw scans PCI and takes ~1 second.
    """
    # lshw is slow (~1 s); can't use the shared _run() which caps at 0.7 s.
    out = _run(["lshw", "-class", "memory"], timeout=3.0)
    if not out:
        return ""

    # Language-independent: match manufacturer/fabriquant/hersteller/fabricante...
    # Only look for actual DRAM keywords (avoid Intel PMC / chipset entries).
    _SKIP_PRODUCTS = re.compile(
        r"(Shared SRAM|PMC|SSRAM|Controller|Chipset)", re.IGNORECASE
    )
    _MFR_PATTERN = re.compile(
        r"^\s*(?:manufacturer|fabriquant|hersteller|fabricante|produttore|"
        r"manufacturer\s*id|mfr):\s*(.+)$",
        re.MULTILINE | re.IGNORECASE,
    )

    # Split into per-device blocks so we can skip non-DRAM entries
    blocks = re.split(r"\*-memory", out)
    for block in blocks:
        if _SKIP_PRODUCTS.search(block):
            continue
        m = _MFR_PATTERN.search(block)
        if m:
            value = m.group(1).strip()
            if value and value not in _MANU_INVALID:
                return value
    return ""


# ---------------------------------------------------------------------------
# Method 4: DDR5 SPD5118 via /dev/i2c-N (user must be in 'i2c' group)
# ---------------------------------------------------------------------------

_I2C_SLAVE = 0x0703   # ioctl request to set slave address

def _try_spd_i2c() -> str:
    """Read DDR5 module manufacturer from SPD5118 EEPROM via /dev/i2c-N.

    Requires the user to be in the 'i2c' group:
      sudo usermod -a -G i2c YOUR_USER  (then log out and back in)

    DDR5 SPD: module manufacturer ID is at bytes 512-513 (JEDEC JEP106).
      Byte 512: number of 0x7F continuation codes (= bank index)
      Byte 513: manufacturer ID byte (MSB = odd parity)
    """
    for name_path in glob.glob("/sys/bus/i2c/devices/*/name"):
        try:
            if open(name_path).read().strip() != "spd5118":
                continue
            dev_dir  = os.path.basename(os.path.dirname(name_path))  # e.g. "21-0050"
            bus_str, addr_str = dev_dir.split("-", 1)
            bus  = int(bus_str)
            addr = int(addr_str, 16)

            fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
            try:
                fcntl.ioctl(fd, _I2C_SLAVE, addr)
                # 2-byte big-endian address for DDR5 SPD (byte 512 = 0x0200)
                os.write(fd, struct.pack(">H", 0x0200))
                data = os.read(fd, 2)
            finally:
                os.close(fd)

            if len(data) < 2:
                continue

            bank   = data[0] & 0x7F   # strip parity bit
            raw_b1 = data[1]
            name   = (
                _JEDEC_MANUFACTURERS.get((bank, raw_b1))
                or _JEDEC_MANUFACTURERS.get((bank, raw_b1 & 0x7F))
            )
            if name:
                return name
        except Exception:
            pass
    return ""


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize_manu(manu: str) -> str:
    """Apply known manufacturer name normalizations."""
    for old, new in _MANU_NORMALIZATIONS.items():
        if old.lower() in manu.lower():
            manu = re.sub(re.escape(old), new, manu, flags=re.IGNORECASE)
    return manu.strip()
