#!/usr/bin/env python3
"""
screen.py — AtomMan X7 Ti LCD daemon.

Handles the serial protocol, tile payload rotation, optional live dashboard,
and the unlock/activation sequence.

Hardware:
  - AtomMan screen : /dev/serial/by-id/usb-Synwit_USB_Virtual_COM-if00
  - Primary GPU    : AMD Radeon RX 7900 XTX  (shown on AtomMan screen if present)
  - Secondary GPU  : Intel Arc Graphics       (shown on AtomMan screen if no AMD)

Protocol summary:
  ENQ  (device→host) : AA 05 <SEQ> CC 33 C3 3C
  Reply (host→device): AA <TileID> 00 <SEQ> {ASCII payload} CC 33 C3 3C
"""
import argparse
import os
import signal
import sys
import time

import serial

import colors
from config import (
    PORT, BAUD, RTSCTS, DSRDTR, TRAILER,
    DEFAULT_WAIT_START, UNLOCK_WINDOW, POST_WRITE_SLEEP,
    OW_API_KEY, OW_UNITS, WEATHER_REFRESH_SECONDS,
    GPU_TILE_USEAGE,
    DISK_MODE, DISK_ROTATE_SECONDS,
)
from colors import colorize, temp_color, util_color, usage_color, C
from cpu    import cpu_model, cpu_usage_pct, cpu_temp_c, cpu_freq_khz, fan_cpu_rpm
from gpu    import gpu_info, gpu_vram_info, get_intel_arc_info, intel_igpu_mem_info, detect_gpu_vendor
from ram    import mem_info, ram_label
from disk   import (
    disk_numbers, disk_temp_c, disk_label,
    discover_disks, disk_numbers_for, _disk_temp_for,
    aggregate_disk_numbers, aggregate_disk_label,
)
from weather import get_weather_cached, get_weather_age_s
from network import NetMeter, fmt_rate


# ---------------------------------------------------------------------------
# Tile IDs (AtomMan firmware constants)
# ---------------------------------------------------------------------------

CPU = 0x53
GPU = 0x36
MEM = 0x49
DSK = 0x4F
DAT = 0x6B
NET = 0x27

# SEQ byte to send for each tile in steady-state
SEQ_FOR = {CPU: '2', GPU: '3', MEM: '4', DSK: '5', DAT: '6', NET: '7'}


def seq_for(tile_id: int) -> int:
    return ord(SEQ_FOR.get(tile_id, '2'))


# ---------------------------------------------------------------------------
# Tile payload builders
# ---------------------------------------------------------------------------

# Network meter (module-level singleton)
_nm        = NetMeter()
_last_net  = {"rxk": None, "txk": None, "rpm": None}


def _week_day(t) -> int:
    """Convert Python weekday (Mon=0) to AtomMan format (Sun=0)."""
    return (t.tm_wday + 1) % 7


def p_cpu() -> str:
    t = cpu_temp_c()
    return (f"{{CPU:{cpu_model()};Tempr:{t};"
            f"Useage:{cpu_usage_pct()};Freq:{cpu_freq_khz()};Tempr1:{t};}}")


def p_gpu() -> str:
    """GPU tile: shows AMD if present, otherwise Intel Arc.

    The Useage field is controlled by GPU_TILE_USEAGE in config.py:
      "vram" → VRAM/RAM occupation % (default)
      "busy" → GPU engine busy %
    """
    if detect_gpu_vendor() == "amd":
        name, temp, busy_pct = gpu_info()
        vram_used, vram_total = gpu_vram_info()
        vram_pct = int(vram_used * 100 / vram_total) if vram_total else 0
    else:
        name, temp, busy_pct = get_intel_arc_info()
        mem_used, mem_total = intel_igpu_mem_info()
        vram_pct = int(mem_used * 100 / mem_total) if mem_total else 0

    useage = vram_pct if GPU_TILE_USEAGE == "vram" else busy_pct
    return f"{{GPU:{name};Tempr:{temp};Useage:{useage}}}"


def p_mem() -> str:
    used, avail, total, usage = mem_info()
    manu  = ram_label()
    label = f"Memory ({manu})" if manu else "Memory"
    return f"{{Memory:{label};Used:{used};Available:{avail};Total:{total};Useage:{usage}}}"


# Rotation state for DISK_MODE="rotate" (idx -1 → first call picks disk 0).
# NOTE: the AtomMan firmware latches DiskName once per tile lifetime — even a
# CLEAR does not re-register it. So in rotate mode the numbers alternate but
# the tile keeps a single combined short label that is valid for both disks.
_disk_rotate = {"idx": -1, "ts": 0.0}


def p_dsk() -> str:
    """Disk tile: reports every internal disk according to DISK_MODE.

    "aggregate" → one logical drive: sums of used/total, hottest temp,
                  combined short label (e.g. "Kingston+WD").
    "rotate"    → one disk at a time, switching every DISK_ROTATE_SECONDS.
    Falls back to the legacy single-disk path when nothing is detected.
    """
    disks = discover_disks()

    if not disks:
        used, total, usage = disk_numbers()
        return (f"{{DiskName:{disk_label() or 'Disk'};Tempr:{disk_temp_c()};"
                f"UsageSpace:{used};AllSpace:{total};Usage:{usage}}}")

    if DISK_MODE == "rotate" and len(disks) > 1:
        now = time.time()
        if _disk_rotate["idx"] < 0 or now - _disk_rotate["ts"] >= DISK_ROTATE_SECONDS:
            _disk_rotate["idx"] = (_disk_rotate["idx"] + 1) % len(disks)
            _disk_rotate["ts"]  = now
        d                  = disks[_disk_rotate["idx"]]
        used, total, usage = disk_numbers_for(d)
        temp               = _disk_temp_for(d["name"])
        # DiskName cannot be re-registered after the first write (firmware
        # limit): use the combined label so it stays valid for both disks.
        label              = aggregate_disk_label(disks)
    else:
        used, total, usage = aggregate_disk_numbers(disks)
        label              = aggregate_disk_label(disks)
        temp               = max(_disk_temp_for(d["name"]) for d in disks)

    return (f"{{DiskName:{label};Tempr:{temp};"
            f"UsageSpace:{used};AllSpace:{total};Usage:{usage}}}")


def p_date() -> str:
    t    = time.localtime()
    week = _week_day(t)
    w    = get_weather_cached()
    base = (f"{{Date:{t.tm_year:04d}/{t.tm_mon:02d}/{t.tm_mday:02d};"
            f"Time:{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d};Week:{week};")
    if w:
        return (base +
                f"Weather:{w['weatherN']};TemprLo:{w['lo']},TemprHi:{w['hi']},"
                f"Zone:{w['zone']},Desc:{w['desc']}}}")
    return base + "Weather:;TemprLo:,TemprHi:,Zone:,Desc:}"


def p_net() -> str:
    rxk, txk = _nm.rates_ks()
    rpm      = fan_cpu_rpm()
    _last_net.update({"rxk": rxk, "txk": txk, "rpm": rpm})

    speed = rpm if (rpm is not None and rpm > 0) else 0
    if rxk is None or txk is None:
        return f"{{SPEED:{speed};NETWORK:N/A,N/A}}"
    return f"{{SPEED:{speed};NETWORK:{fmt_rate(rxk)},{fmt_rate(txk)}}}"


# Tile rotation used during the unlock phase
UNLOCK_ROT = [(CPU, p_cpu), (GPU, p_gpu), (MEM, p_mem)]

# Tile rotation for steady-state operation (no VOL/BAT — mini PC)
FULL_ROT = [
    (CPU, p_cpu), (GPU, p_gpu), (MEM, p_mem),
    (DSK, p_dsk), (DAT, p_date),    (NET, p_net),
]


# ---------------------------------------------------------------------------
# Protocol primitives
# ---------------------------------------------------------------------------

def read_enq(ser) -> int | None:
    """Read one ENQ frame from the screen. Returns the SEQ byte or None.

    All serial errors are caught and returned as None so callers never
    have to handle SerialException; the device disconnect will surface
    on the next write attempt instead.
    """
    try:
        if ser.read(1) != b"\xAA":
            return None
        if ser.read(1) != b"\x05":
            return None
        b3 = ser.read(1)
        if not b3:
            return None
        if ser.read(4) != TRAILER:
            return None
        return b3[0]
    except Exception:
        return None


def build_reply(tile_id: int, seq: int, payload: str) -> bytes:
    return bytes([0xAA, tile_id, 0x00, seq]) + payload.encode("latin-1", "ignore") + TRAILER


def open_serial(wait_start: float) -> serial.Serial:
    time.sleep(wait_start)
    s = serial.Serial(PORT, BAUD, timeout=1.0, write_timeout=1.0,
                      dsrdtr=DSRDTR, rtscts=RTSCTS)
    try:
        s.reset_input_buffer()
        s.reset_output_buffer()
    except Exception:
        pass
    return s


def _is_ascii_seq(b: int) -> bool:
    return (0x30 <= b <= 0x39) or b == 0x3C


# ---------------------------------------------------------------------------
# Dashboard (diff-based, redraws only when data changes)
# ---------------------------------------------------------------------------

_dashboard_last_hash = -1


def render_dashboard(latest: dict) -> None:
    global _dashboard_last_hash

    current_hash = hash(str(sorted(latest.items())))
    if current_hash == _dashboard_last_hash:
        return  # nothing changed, skip redraw

    sys.stdout.write("\033[2J\033[H")
    t = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{colorize('AtomMan — Active', C.BW)}   Time: {colorize(t, C.BC)}")
    print("-" * 72)

    # --- CPU ---
    tc = temp_color(latest.get("cpu_temp", "?"))
    uc = util_color(latest.get("cpu_usage", "?"))
    print(f"Processor type : {latest.get('cpu_model', '')}")
    print(f"Processor temp : {tc(str(latest.get('cpu_temp', '?')) + ' °C')}")
    print(f"CPU usage      : {uc(str(latest.get('cpu_usage', '?')) + ' %')}")
    print(f"CPU freq       : {latest.get('cpu_freq_khz', '?')} kHz")
    print()

    # --- GPU: AMD Radeon (primary, dashboard) ---
    gtc = temp_color(latest.get("gpu_temp", 0))
    guc = util_color(latest.get("gpu_util", 0))
    print(f"GPU model      : {latest.get('gpu_name', 'N/A')}")
    print(f"GPU temp       : {gtc(str(latest.get('gpu_temp', '0')) + ' °C')}")
    print(f"GPU usage      : {guc(str(latest.get('gpu_util', '0')) + ' %')}")
    vram_used  = latest.get("gpu_vram_used", 0)
    vram_total = latest.get("gpu_vram_total", 0)
    if vram_total > 0:
        vram_pct = int(round(100.0 * vram_used / vram_total))
        vvc = usage_color(vram_pct)
        print(f"GPU VRAM       : {vram_used} / {vram_total} MB  ({vvc(str(vram_pct) + ' %')})")
    print()

    # --- GPU: Intel Arc (secondary, shown on AtomMan screen) ---
    if latest.get("intel_name"):
        itc = temp_color(latest.get("intel_temp", 0))
        iuc = util_color(latest.get("intel_util", 0))
        print(f"Intel GPU      : {latest.get('intel_name')}")
        print(f"Intel temp     : {itc(str(latest.get('intel_temp', '0')) + ' °C')}")
        if latest.get("intel_util", 0) > 0:
            print(f"Intel usage    : {iuc(str(latest.get('intel_util', '0')) + ' %')}")
        imem_used  = latest.get("intel_mem_used", 0)
        imem_total = latest.get("intel_mem_total", 0)
        if imem_total > 0:
            imem_pct = int(round(100.0 * imem_used / imem_total))
            imc = usage_color(imem_pct)
            print(f"Intel RAM      : {imem_used} MB / {imem_total // 1024} GB  ({imc(str(imem_pct) + ' %')})")
        print()

    # --- RAM ---
    muc = usage_color(latest.get("mem_usage", "?"))
    print(f"RAM (vendor)   : {latest.get('ram_vendor', '')}")
    print(f"RAM used       : {latest.get('mem_used', '?')} GB")
    print(f"RAM avail      : {latest.get('mem_avail', '?')} GB")
    print(f"RAM total      : {latest.get('mem_total', '?')} GB")
    print(f"RAM usage      : {muc(str(latest.get('mem_usage', '?')) + ' %')}")
    print()

    # --- Disk (per-disk detail + aggregate total) ---
    per_disk = latest.get("disks") or []
    if per_disk:
        for i, d in enumerate(per_disk, 1):
            duc = usage_color(d["usage"])
            dtc = temp_color(d["temp"])
            print(f"Disk {i} ({d['model']})")
            print(f"  Disk temp  : {dtc(str(d['temp']) + ' °C')}")
            print(f"  Disk used  : {d['used']} GB")
            print(f"  Disk total : {d['total']} GB")
            print(f"  Disk usage : {duc(str(d['usage']) + ' %')}")
        tuc = usage_color(latest.get("disk_usage", "?"))
        ttc = temp_color(latest.get("disk_temp", 0))
        print(f"Disk total   : {latest.get('disk_used', '?')} / "
              f"{latest.get('disk_total', '?')} GB  "
              f"({tuc(str(latest.get('disk_usage', '?')) + ' %')}) "
              f"max temp {ttc(str(latest.get('disk_temp', '0')) + ' °C')}")
    else:
        duc = usage_color(latest.get("disk_usage", "?"))
        dtc = temp_color(latest.get("disk_temp", 0))
        print(f"Disk (label) : {latest.get('disk_label', '')}")
        print(f"Disk temp    : {dtc(str(latest.get('disk_temp', '0')) + ' °C')}")
        print(f"Disk used    : {latest.get('disk_used', '?')} GB")
        print(f"Disk total   : {latest.get('disk_total', '?')} GB")
        print(f"Disk usage   : {duc(str(latest.get('disk_usage', '?')) + ' %')}")
    print()

    # --- Network ---
    print(f"Net iface      : {latest.get('iface', 'N/A')}")
    print(f"Net RX,TX      : {fmt_rate(latest.get('net_rx'))}, {fmt_rate(latest.get('net_tx'))}")
    fan_rpm = latest.get("fan_rpm")
    if fan_rpm and fan_rpm > 0:
        print(f"Fan speed      : {fan_rpm} r/min")
    print()

    # --- Weather ---
    w = get_weather_cached()
    if w:
        unit = "°C" if OW_UNITS == "metric" else "°F"
        print(colorize("Weather        : ONLINE", C.BG))
        print(f"  Code         : {w['weatherN']} (mapped)")
        print(f"  Lo/Hi        : {w['lo']}/{w['hi']} {unit}")
        print(f"  Zone         : {w.get('zone_display', w['zone'])}")
        print(f"  Desc         : {w['desc']}")
        print(f"  Age          : {get_weather_age_s()}s (refresh {WEATHER_REFRESH_SECONDS}s)")
    else:
        reason = "no API key" if not OW_API_KEY.strip() else "offline / unavailable"
        print(colorize(f"Weather        : OFFLINE ({reason})", C.BY))

    print("-" * 72)
    sys.stdout.flush()

    _dashboard_last_hash = current_hash


# ---------------------------------------------------------------------------
# latest dict updater (called after each tile write)
# ---------------------------------------------------------------------------

def _update_latest(tile_id: int, latest: dict) -> None:
    """Refresh the `latest` dict with current readings for the given tile."""
    if tile_id == CPU:
        latest.update({
            "cpu_model":    cpu_model(),
            "cpu_temp":     cpu_temp_c(),
            "cpu_usage":    cpu_usage_pct(),
            "cpu_freq_khz": cpu_freq_khz(),
        })

    elif tile_id == GPU:
        vendor = detect_gpu_vendor()
        if vendor == "amd":
            name, temp, util = gpu_info()
            vram_used, vram_total = gpu_vram_info()
            iname, itemp, iutil  = get_intel_arc_info()
            imem_used, imem_total = intel_igpu_mem_info()
            latest.update({
                "gpu_name": name, "gpu_temp": temp, "gpu_util": util,
                "gpu_vram_used": vram_used, "gpu_vram_total": vram_total,
                "intel_name": iname, "intel_temp": itemp, "intel_util": iutil,
                "intel_mem_used": imem_used, "intel_mem_total": imem_total,
            })
        elif vendor == "intel":
            iname, itemp, iutil = get_intel_arc_info()
            imem_used, imem_total = intel_igpu_mem_info()
            latest.update({
                "gpu_name": iname, "gpu_temp": itemp, "gpu_util": iutil,
                "intel_name": iname, "intel_temp": itemp, "intel_util": iutil,
                "intel_mem_used": imem_used, "intel_mem_total": imem_total,
            })
        else:
            name, temp, util = gpu_info()
            latest.update({"gpu_name": name, "gpu_temp": temp, "gpu_util": util})

    elif tile_id == MEM:
        used, avail, total, usage = mem_info()
        latest.update({
            "ram_vendor": ram_label() or "",
            "mem_used": used, "mem_avail": avail,
            "mem_total": total, "mem_usage": usage,
        })

    elif tile_id == DSK:
        disks    = discover_disks()
        per_disk = []
        for d in disks:
            u, t, pc = disk_numbers_for(d)
            per_disk.append({
                "name":  d["name"],
                "model": d["model"] or d["name"],
                "used":  u, "total": t, "usage": pc,
                "temp":  _disk_temp_for(d["name"]),
            })

        if per_disk:
            au, at, apc = aggregate_disk_numbers(disks)
            latest.update({
                "disks":      per_disk,
                "disk_label": aggregate_disk_label(disks),
                "disk_temp":  max(p["temp"] for p in per_disk),
                "disk_used":  au, "disk_total": at, "disk_usage": apc,
            })
        else:
            used, total, usage = disk_numbers()
            latest.update({
                "disks":      [],
                "disk_label": disk_label() or "Disk",
                "disk_temp":  disk_temp_c(),
                "disk_used":  used, "disk_total": total, "disk_usage": usage,
            })

    elif tile_id == NET:
        rxk = _last_net.get("rxk")
        txk = _last_net.get("txk")
        rpm = _last_net.get("rpm")
        if rxk is None or txk is None:
            rxk, txk = _nm.rates_ks()
            rpm = fan_cpu_rpm()
            _last_net.update({"rxk": rxk, "txk": txk, "rpm": rpm})
        latest.update({
            "net_rx": rxk, "net_tx": txk,
            "fan_rpm": rpm, "iface": _nm.iface or "N/A",
        })

    elif tile_id == DAT:
        get_weather_cached()  # nudge the weather cache on every DATE cycle


# ---------------------------------------------------------------------------
# Unlock / activation sequence
# ---------------------------------------------------------------------------

def _force_reset(ser) -> None:
    """Toggle DTR/RTS to hardware-reset the AtomMan display."""
    print("[Reset] Forcing screen reset via DTR/RTS…")
    for _ in range(5):
        ser.setDTR(False); time.sleep(0.1)
        ser.setDTR(True);  time.sleep(0.05)
    for _ in range(3):
        ser.setRTS(False); time.sleep(0.1)
        ser.setRTS(True);  time.sleep(0.05)
    print("[Reset] Done.")


def _unlock_attempt(ser, attempt_idx: int, latest: dict,
                    unlock_window: float, dashboard: bool) -> bool:
    print(f"[Attempt {attempt_idx}/3] Unlock window {unlock_window:.0f}s")

    # On the first attempt, send CLEAR to all tiles to wake the firmware
    # and trigger ENQ responses (mirrors the original screen.py behaviour).
    if attempt_idx == 1:
        print("[Reset] Sending screen clear/reset commands...")
        for tile_id in (CPU, GPU, MEM, DSK, DAT, NET):
            frm = build_reply(tile_id, ord('2'), "CLEAR")
            ser.write(frm)
            time.sleep(0.01)
        ser.flush()
        print("[Reset] Clear commands sent.")

    start        = time.time()
    idx          = 0
    boot_replies = 0
    enq_times    = []
    last_render  = 0.0

    while time.time() - start < unlock_window:
        seq = read_enq(ser)
        if seq is None:
            if dashboard and (time.time() - last_render > 1.0):
                render_dashboard(latest)
                last_render = time.time()
            continue

        now = time.time()
        enq_times = [t for t in enq_times if now - t <= 2.0]
        enq_times.append(now)

        tile, maker = UNLOCK_ROT[idx % len(UNLOCK_ROT)]
        payload     = maker()
        ser.write(build_reply(tile, seq, payload))
        ser.flush()
        time.sleep(POST_WRITE_SLEEP)

        _update_latest(tile, latest)
        idx += 1

        if _is_ascii_seq(seq):
            boot_replies += 1

        if boot_replies >= 3 and len(enq_times) >= 5:
            print(f"[Attempt {attempt_idx}] Activated.")
            return True

    print(f"[Attempt {attempt_idx}] No activation within window.")
    return False


# ---------------------------------------------------------------------------
# Signal handler
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    print(f"\n[AtomMan] Signal {signum} received — shutting down…")


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main():
    global _shutdown_requested

    ap = argparse.ArgumentParser(
        description="AtomMan X7 Ti LCD daemon",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--attempts",    type=int,   default=3,
                    help="Number of unlock attempts before giving up")
    ap.add_argument("--window",      type=float, default=UNLOCK_WINDOW,
                    help="Seconds per unlock attempt")
    ap.add_argument("--no-color",    action="store_true",
                    help="Disable ANSI colors in the dashboard")
    ap.add_argument("--dashboard",   action="store_true",
                    help="Show a live dashboard in the terminal")
    ap.add_argument("--start-delay", type=float, default=DEFAULT_WAIT_START,
                    help="Seconds to wait before opening the serial port")
    args = ap.parse_args()

    colors.NOCOLOR = args.no_color

    ser = open_serial(args.start_delay)
    print(f"[AtomMan] {PORT} @ {BAUD} baud  (DSRDTR={DSRDTR} RTSCTS={RTSCTS})")
    print(f"[AtomMan] GPU order: AMD→Intel→NVIDIA | Fan: CPU hwmon only")

    latest = {"cpu_model": cpu_model()}

    # Pre-populate all fields so the dashboard never shows '?' on first render.
    _update_latest(CPU, latest)
    _update_latest(GPU, latest)
    _update_latest(MEM, latest)
    _update_latest(DSK, latest)
    _update_latest(NET, latest)

    _force_reset(ser)

    activated = False
    for i in range(1, args.attempts + 1):
        activated = _unlock_attempt(ser, i, latest, args.window, args.dashboard)
        if activated:
            break
        try:
            ser.setDTR(False); time.sleep(0.05); ser.setDTR(True)
        except Exception:
            pass
        time.sleep(0.3)

    if not activated:
        print("[WARN] Activation timed out — continuing in steady state anyway.")
    else:
        print("[OK] Screen activated.")

    idx         = 0
    last_render = 0.0

    while not _shutdown_requested:
        enq = read_enq(ser)

        if enq is None:
            if args.dashboard and (time.time() - last_render > 1.0):
                render_dashboard(latest)
                last_render = time.time()
            time.sleep(0.005)
            continue

        tile, maker = FULL_ROT[idx % len(FULL_ROT)]
        payload     = maker()
        ser.write(build_reply(tile, seq_for(tile), payload))
        ser.flush()
        time.sleep(POST_WRITE_SLEEP)

        _update_latest(tile, latest)

        if args.dashboard:
            now = time.time()
            if now - last_render >= 0.25:
                render_dashboard(latest)
                last_render = now

        idx = (idx + 1) % 1_000_000

    print("[AtomMan] Shutting down.")
    ser.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT,  _signal_handler)
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[AtomMan] Fatal error: {e}")
        sys.exit(1)
