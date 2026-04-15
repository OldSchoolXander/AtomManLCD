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
| Primary GPU | AMD Radeon RX 7900 XTX (amdgpu driver) — *tested card* |
| Secondary GPU (iGPU) | Intel Arc Graphics (i915 driver) |
| RAM | 96 GB DDR5 |
| LCD panel | USB serial — `/dev/serial/by-id/usb-Synwit_USB_Virtual_COM-if00` |

The LCD panel is internally wired to the Intel Arc iGPU.  When an external
AMD GPU is present, it is shown on the LCD GPU tile (it is the main workload
GPU).  If no AMD GPU is detected, the Intel Arc is shown instead.

---

## Architecture

The code is split into focused, independently-testable modules:

| File | Responsibility |
|---|---|
| `screen.py` | Serial protocol, tile rotation, unlock sequence, dashboard |
| `gpu.py` | AMD and Intel Arc GPU metrics (temp, VRAM%, name) |
| `cpu.py` | CPU model, usage %, temperature, frequency, fan RPM |
| `ram.py` | RAM used/available/total, vendor detection |
| `disk.py` | Disk usage, temperature, label |
| `network.py` | Network throughput (RX/TX) with auto-scaling |
| `weather.py` | Weather fetching (Open-Meteo + OpenWeather fallback) |
| `colors.py` | ANSI color helpers for the dashboard |
| `utils.py` | Shared `_run()` / `_read()` helpers |
| `config.py` | All tunables in one place |

---

## GPU handling

### LCD tile

The GPU tile on the LCD always shows the **primary workload GPU**:

- If an AMD GPU is detected → AMD name, temperature, VRAM occupation %
- Otherwise → Intel Arc name, temperature, RAM occupation %

GPU card slot assignment (`card0`, `card1`, …) is resolved dynamically at
runtime by scanning `/sys/class/drm/card*/device/vendor`.  The result is
cached permanently so there is no overhead per cycle.  This makes the driver
resilient to cardN renumbering across reboots.

### GPU tile Useage field

The `Useage` value sent to the LCD is configurable via `GPU_TILE_USEAGE` in
`config.py`:

| Value | Description |
|---|---|
| `"vram"` *(default)* | VRAM/RAM occupation % — reliable for all workload types including Vulkan/compute |
| `"busy"` | GPU engine busy % — AMD: `gpu_busy_percent` sysfs / Intel: `drm-engine-render` delta via fdinfo |

Can also be set at runtime via the environment variable
`ATOMMAN_GPU_TILE_USEAGE`.

### AMD VRAM tracking

VRAM usage is read from DRM fdinfo (`/proc/*/fdinfo/*`, field
`drm-memory-vram: X KiB`), not from sysfs `mem_info_vram_used`.  The fdinfo
approach correctly tracks all DRM Buffer Objects including those allocated by
**radv (Vulkan)**, OpenGL, and Vulkan compute — which sysfs misses entirely.

The service must run as root to scan all processes' fdinfo.

> **Compatibility:** requires the `amdgpu` driver and the DRM fdinfo interface
> introduced in **Linux 5.19**.  Works with any AMD GPU using `amdgpu` + `radv`.

### AMD GPU name

`product_name` and `name` sysfs files are absent on some cards (including the
RX 7900 XTX).  The driver falls back to `lspci -mm`, preferring the subsystem
device name (e.g. `NITRO+ RX 7900 XTX Vapor-X`) over the generic device name.

### Intel Arc memory tracking

Memory usage is read from DRM fdinfo field `drm-total-system0: X KiB`
(system RAM allocated as GPU memory).  Values are in KiB and converted with
a single `// 1024` division.  Memory total is set to 50% of system RAM as a
practical upper bound for iGPU allocation.

### Intel Arc temperature

Sensor priority:

1. `i915` native hwmon on the Intel card (`<intel_card>/device/hwmon/hwmon*/temp1_input`)
2. `coretemp "Package id 0"` — SoC-wide proxy, used as fallback

An EMA (α = 0.5) is applied to suppress transient spikes caused by CPU bursts
on the shared Meteor Lake SoC die.

### Terminal dashboard

The dashboard displays both GPUs when both are present:

- **AMD GPU** — name, temperature, engine busy %, VRAM used/total/occupation %
- **Intel Arc** — name, temperature (EMA-smoothed), RAM used/total/occupation %

The dashboard redraws only when data changes (hash comparison), eliminating
unnecessary flicker.

---

## Weather

- **Primary source:** [Open-Meteo](https://open-meteo.com/) — free, no API key required.
- **Fallback:** [OpenWeather One Call API](https://openweathermap.org/api) —
  used only when Open-Meteo fails and `OW_API_KEY` is set in `config.py`.

Day/night icon codes are selected automatically using the `is_day` field from
the Open-Meteo `current` endpoint:

| WMO code | Condition | Day icon | Night icon |
|---|---|---|---|
| 0 | Clear sky | 1 | 3 |
| 1 | Mainly clear | 5 | 6 |
| 2 | Partly cloudy | 7 | 8 |
| 3+ | Overcast / rain / snow / storm | unchanged | unchanged |

---

## Requirements

```
Python 3.10+
pyserial
lspci (pciutils)
```

The service must run as **root** to read `/proc/*/fdinfo/*` for all processes.

```bash
pip install pyserial
sudo apt install pciutils
```

---

## Configuration

Edit `config.py`:

| Variable | Default | Description |
|---|---|---|
| `PORT` | `/dev/serial/by-id/usb-Synwit_USB_Virtual_COM-if00` | Serial port |
| `BAUD` | `115200` | Baud rate |
| `GPU_TILE_USEAGE` | `"vram"` | Metric for GPU tile: `"vram"` or `"busy"` |
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
| GPU | `0x36` | `{GPU:NITRO+ RX 7900 XTX Vapor-X;Tempr:52;Useage:97}` |
| MEM | `0x49` | `{Memory:Micron DDR5;Used:18.4;Available:74.2;Total:92.6;Useage:20}` |
| DISK | `0x4F` | `{DiskName:Samsung 990 Pro;Tempr:41;UsageSpace:512;AllSpace:1907;Usage:27}` |
| DATE | `0x6B` | `{Date:2026/04/15;Time:22:10:00;Week:4;Weather:3;TemprLo:9,TemprHi:21,Zone:Paris,Desc:clear sky}` |
| NET | `0x27` | `{SPEED:1200;NETWORK:3.2M/s,512K/s}` |

---

## License

MIT — see original repository [RamSet/AtomMan](https://github.com/RamSet/AtomMan).
