"""
config.py — All user-configurable settings and environment variable overrides.
Edit this file to change weather location, serial port, timing, etc.
"""
import os

# ===================== Weather Settings =====================
# Open-Meteo is the primary source and requires NO API key.
# OW_API_KEY is optional — if set, OpenWeather is used as a fallback
# when Open-Meteo is unreachable. Leave empty for Open-Meteo only.
OW_API_KEY  = ""               # optional — e.g. "abcdef123456..."
OW_LOCATION = "77164,fr"       # city | "zip,country" | "lat,lon"
OW_UNITS    = "metric"         # "metric" (°C) or "imperial" (°F)
OW_LANG     = "en"             # language for OpenWeather descriptions (fallback only)

# How often (in seconds) to refresh weather data from the API.
# Override with env var: ATOMMAN_WEATHER_REFRESH=300
WEATHER_REFRESH_SECONDS = int(os.getenv("ATOMMAN_WEATHER_REFRESH", "600"))

# ===================== Serial Port =====================
PORT    = os.getenv("ATOMMAN_PORT", "/dev/serial/by-id/usb-Synwit_USB_Virtual_COM-if00")
BAUD    = int(os.getenv("ATOMMAN_BAUD", "115120").replace("115120", "115200"))
RTSCTS  = os.getenv("ATOMMAN_RTSCTS", "false").lower() in ("1", "true", "yes", "on")
DSRDTR  = os.getenv("ATOMMAN_DSRDTR", "true").lower()  in ("1", "true", "yes", "on")

# Protocol trailer bytes (fixed by AtomMan firmware).
TRAILER = b"\xCC\x33\xC3\x3C"

# ===================== Timing =====================
# Seconds to wait before opening the serial port (lets USB CDC settle).
DEFAULT_WAIT_START = float(os.getenv("ATOMMAN_WAIT_START", "3.0"))

# Seconds allowed per unlock attempt.
UNLOCK_WINDOW = float(os.getenv("ATOMMAN_UNLOCK_SECONDS", "5.0"))

# Sleep after each serial write (avoids flooding the firmware).
POST_WRITE_SLEEP = float(os.getenv("ATOMMAN_WRITE_SLEEP", "0.006"))

# ===================== GPU Tile =====================
# Metric shown in the Useage field of the GPU LCD tile.
#   "vram" → VRAM/RAM occupation % — reliable for all GPU types and workloads
#            (AMD: drm-memory-vram via fdinfo / Intel: drm-total-system0 via fdinfo)
#   "busy" → GPU engine busy % — AMD: gpu_busy_percent sysfs (may read 0 for
#            Vulkan/compute workloads) / Intel: drm-engine-render delta via fdinfo
GPU_TILE_USEAGE = os.getenv("ATOMMAN_GPU_TILE_USEAGE", "vram")

# ===================== Disk Tile (multi-SSD) =====================
# How the Disk LCD tile reports multiple internal disks:
#   "aggregate" → one logical drive: used/total = sum of all disks,
#                 temp = hottest disk, label = combined short models.
#   "rotate"    → show one disk at a time on the LCD, alternating every
#                 DISK_ROTATE_SECONDS seconds.
# Override with env var: ATOMMAN_DISK_MODE=rotate
DISK_MODE = os.getenv("ATOMMAN_DISK_MODE", "aggregate").lower()

# Seconds each disk stays visible in "rotate" mode.
# Override with env var: ATOMMAN_DISK_ROTATE=10.0
DISK_ROTATE_SECONDS = float(os.getenv("ATOMMAN_DISK_ROTATE", "10.0"))
