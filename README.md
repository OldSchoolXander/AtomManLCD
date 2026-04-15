# AtomMan X7 Ti — LCD Driver (Enhanced Fork)

A Python daemon that drives the built-in USB LCD panel of the
**Minisforum AtomMan X7 Ti** mini PC, displaying live system metrics across
six hardware tiles.

---

## Credits

This project is a fork of
[RamSet/AtomMan](https://github.com/RamSet/AtomMan) by **RamSet**, who did
the initial reverse-engineering of the AtomMan serial protocol and built the
first working Linux driver.  All protocol knowledge (tile IDs, ENQ/REPLY
framing, payload format, firmware icon codes) originates from that work.

---

## Target hardware

| Component | Details |
|---|---|
| Machine | Minisforum AtomMan X7 Ti |
| CPU | Intel Core Ultra 9 185H (Meteor Lake) |
| Primary GPU | AMD Radeon RX 7900 XTX (amdgpu driver) — *test card* |
| Secondary GPU (iGPU) | Intel Arc Graphics (i915 driver) |
| RAM | 96 GB DDR5 |
| LCD panel | USB serial — `/dev/serial/by-id/usb-Synwit_USB_Virtual_COM-if00` |

The LCD is internally wired to the Intel Arc iGPU.  The AMD Radeon is the
primary display GPU and is shown in the dashboard only.

---

## Architecture

The original project was a single monolithic `screen.py`.  This fork splits
the code into focused, independently-testable modules:

| File | Responsibility |
|---|---|
| `screen.py` | Serial protocol, tile rotation, unlock sequence, dashboard |
| `gpu.py` | AMD and Intel Arc GPU metrics (temp, util%, VRAM/RAM) |
| `cpu.py` | CPU model, usage %, temperature, frequency, fan RPM |
| `ram.py` | RAM used/available/total, vendor detection |
| `disk.py` | Disk usage, temperature, label |
| `network.py` | Network throughput (RX/TX) with auto-scaling |
| `weather.py` | Weather fetching (Open-Meteo + OpenWeather fallback) |
| `colors.py` | ANSI color helpers for the dashboard |
| `utils.py` | Shared `_run()` / `_read()` helpers |
| `config.py` | All tunables in one place |

---

## Improvements over the original

### 1. Modular code structure

The single-file `screen.py` has been split into the modules listed above.
Each module has its own cache, TTL, and public API, making the codebase
easier to read, test, and extend.

---

### 2. Full Intel Arc iGPU support

The original had no Intel Arc support.  This fork adds three metrics for the
iGPU via the **DRM fdinfo** interface (`/proc/*/fdinfo/*`), which the i915
driver exposes for every DRM client:

- **Utilization %** — computed from `drm-engine-render` cumulative nanoseconds
  delta over a 3-second window, summed across all i915 clients and deduplicated
  by `drm-client-id`.
- **Memory used** — sum of `drm-total-system0` (system RAM allocated as GPU
  memory) across all unique i915 clients.  Unit is KiB (see fix #4 below).
- **Memory total** — set to 50 % of system RAM as a practical upper bound for
  iGPU allocation.

The service runs as root so it can scan all processes' fdinfo, including
KDE Plasma, Xwayland, browsers, etc.

---

### 3. AMD VRAM via DRM fdinfo — correct Vulkan tracking

The original read VRAM usage from
`/sys/class/drm/card0/device/mem_info_vram_used`, which **does not track
Vulkan (radv) allocations**.  Loading a large model in LM Studio via the
Vulkan backend would show only ~50 MB of VRAM used regardless of actual
allocation.

This fork replaces that sysfs read with a DRM fdinfo scan, reading
`drm-memory-vram: X KiB` per amdgpu client and summing them deduplicated by
`drm-client-id`.  This correctly reflects all DRM Buffer Objects including
those allocated by radv (Vulkan), OpenGL, and Vulkan compute workloads.

`mem_info_vram_total` is still read from sysfs (it is a static hardware value
and always accurate).

> **Compatibility:** this mechanism relies on the `amdgpu` kernel driver and
> the DRM fdinfo interface introduced in **Linux 5.19**.  It works with any
> AMD GPU using `amdgpu` + `radv` (Mesa Vulkan driver) — the RX 7900 XTX is
> simply the card this was developed and validated on.

---

### 4. KiB unit bug fix — Intel Arc memory

`drm-total-system0` values in i915 fdinfo are expressed in **KiB**, not bytes.
The original implementation treated them as bytes and divided by 1 024 × 1 024,
producing a result of 0 MB even when KDE Plasma alone allocates ~200 MB on the
Arc iGPU.

Fix: values are now stored as `mem_kib` and converted with a single `// 1024`
division.

---

### 5. Intel Arc temperature — better sensor + EMA smoothing

**Problem:** the previous approach read `coretemp "Package id 0"`, which is the
*maximum* temperature across the entire Meteor Lake SoC (CPU P-cores, E-cores,
SoC tile, …).  Any brief CPU burst spikes this value to 70 °C even when the
GPU is idle, producing misleading peaks on the LCD.

**Fix — sensor selection (priority order):**

1. `i915` native hwmon on the Intel card (`<intel_card>/device/hwmon/hwmon*/temp1_input`) —
   true GPU junction temperature when exposed by the kernel/firmware.
2. `coretemp "Package id 0"` — SoC-wide proxy, used as fallback.

**Fix — EMA smoothing (α = 0.5):**

An exponential moving average is applied to the raw reading.  A single-sample
spike is halved rather than shown verbatim.  A sustained temperature change
converges within 3–4 refresh cycles (~30–40 s with the 10-second cache).

```
Example — CPU burst spike of 70 °C while GPU is at 45 °C:
  Raw (before):  45 → 45 → 70 → 45 → 45   (spike fully visible)
  EMA (after):   45 → 45 → 57 → 51 → 48   (spike absorbed)
```

---

### 6. Weather night icons

The original `_map_wmo_to_weather_n()` function always returned daytime icon
codes, even at night.  The comment in the code acknowledged this as a known
limitation ("Open-Meteo provides no day/night icon").

This fork requests the `is_day` field from the Open-Meteo `current` endpoint
(available in the standard free API) and passes it through the mapping
function.  Night-specific AtomMan firmware icon codes are now used when
`is_day == 0`:

| WMO code | Condition | Day icon | Night icon |
|---|---|---|---|
| 0 | Clear sky | 1 | **3** |
| 1 | Mainly clear | 5 | **6** |
| 2 | Partly cloudy | 7 | **8** |
| 3+ | Overcast / rain / snow / storm | unchanged | unchanged |

WMO codes 1 and 2 were also previously collapsed into the same icon (5).
They are now mapped separately (5/6 for mainly clear, 7/8 for partly cloudy).

The OpenWeather fallback path already handled day/night correctly via the
`d`/`n` icon suffix; it is unchanged.

---

### 7. Dual weather source with automatic fallback

- **Primary:** [Open-Meteo](https://open-meteo.com/) — free, no API key required.
- **Fallback:** [OpenWeather One Call API](https://openweathermap.org/api) —
  used only when Open-Meteo fails and `OW_API_KEY` is set in `config.py`.

---

### 8. Diff-based dashboard

The terminal dashboard redraws only when the data dict actually changes
(hash comparison).  This eliminates unnecessary screen flicker when metrics
are stable.

---

### 9. Dashboard shows both GPUs

The live dashboard displays:

- **AMD Radeon RX 7900 XTX** — name, temperature, utilization %, VRAM used/total/%.
- **Intel Arc Graphics** — name, temperature (EMA-smoothed), utilization %,
  system RAM used/total/%.

The LCD panel itself shows only the Intel Arc tile (it is the GPU the screen
is wired to).

---

### 10. Dynamic GPU card-slot resolution

The original code assumed `card0` = AMD and `card1` = Intel Arc.  The
kernel assigns DRM card indices in probe order, which can change across
reboots (especially after a kernel or firmware update).

This fork introduces `_resolve_card_path(vendor)` in `gpu.py`, which scans
`/sys/class/drm/card*/device/vendor` in sorted order at runtime and returns
the correct card path for the requested vendor (`"amd"` → `0x1002`,
`"intel"` → `0x8086`).  Results are cached for the lifetime of the process.

All sysfs paths that previously hardcoded `card0` or `card1` now go through
this resolver, making the driver resilient to cardN renumbering.

---

## Requirements

```
Python 3.10+
pyserial
```

The service must run as **root** to read `/proc/*/fdinfo/*` for all processes.

```bash
pip install pyserial
```

---

## Configuration

Edit `config.py`:

| Variable | Default | Description |
|---|---|---|
| `PORT` | `/dev/serial/by-id/usb-Synwit_USB_Virtual_COM-if00` | Serial port |
| `BAUD` | `115200` | Baud rate |
| `OW_API_KEY` | `""` | OpenWeather API key (optional) |
| `OW_LOCATION` | `""` | City, `"zip,country"`, or `"lat,lon"` |
| `OW_UNITS` | `"metric"` | `"metric"` or `"imperial"` |
| `OW_LANG` | `"en"` | Language for OpenWeather descriptions |
| `WEATHER_REFRESH_SECONDS` | `600` | Weather cache TTL |
| `DEFAULT_WAIT_START` | `3.0` | Seconds before opening serial port |
| `UNLOCK_WINDOW` | `30.0` | Seconds per unlock attempt |

---

## Systemd service

```ini
# /etc/systemd/system/atomman.service
[Unit]
Description=AtomMan X7 Ti LCD Daemon
After=multi-user.target

[Service]
ExecStart=/usr/bin/python3 /usr/local/src/atomman/screen.py --start-delay 5
WorkingDirectory=/usr/local/src/atomman
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable atomman
sudo systemctl start atomman
journalctl -u atomman -f
```

---

## Protocol reference (from RamSet/AtomMan)

**ENQ** (device → host):
```
AA 05 <SEQ> CC 33 C3 3C
```

**Reply** (host → device):
```
AA <TileID> 00 <SEQ> {ASCII payload} CC 33 C3 3C
```

| Tile | ID | Payload example |
|---|---|---|
| CPU | `0x53` | `{CPU:Intel Core Ultra 9;Tempr:45;Useage:7;Freq:2400000;Tempr1:45;}` |
| GPU | `0x36` | `{GPU:Intel Arc Graphics;Tempr:38;Useage:12}` |
| MEM | `0x49` | `{Memory:Micron DDR5;Used:18.4;Available:74.2;Total:92.6;Useage:20}` |
| DISK | `0x4F` | `{DiskName:Samsung 990 Pro;Tempr:41;UsageSpace:512;AllSpace:1907;Usage:27}` |
| DATE | `0x6B` | `{Date:2026/04/15;Time:22:10:00;Week:4;Weather:3;TemprLo:9,TemprHi:21,Zone:Paris,Desc:clear sky}` |
| NET | `0x27` | `{SPEED:1200;NETWORK:3.2M/s,512K/s}` |

---

## License

MIT — see original repository [RamSet/AtomMan](https://github.com/RamSet/AtomMan).
