"""
weather.py — Weather integration with local caching.

Primary source : Open-Meteo (free, no API key required).
Fallback source: OpenWeather One Call API (requires OW_API_KEY in config.py).

Configuration (edit config.py):
  OW_API_KEY              — optional; enables OpenWeather as fallback.
  OW_LOCATION             — city, "zip,country", or "lat,lon".
  OW_UNITS                — "metric" (°C) or "imperial" (°F).
  OW_LANG                 — language code used only for OpenWeather descriptions.
  WEATHER_REFRESH_SECONDS — how often to refresh from the API (default 600 s).

Public API:
  get_weather_cached()  — dict {weatherN, lo, hi, zone, desc} or None.
  get_weather_age_s()   — seconds since last successful fetch (for dashboard).
"""
import json
import re
import socket
import time
import urllib.parse
import urllib.request

from config import (
    OW_API_KEY, OW_LOCATION, OW_UNITS, OW_LANG, WEATHER_REFRESH_SECONDS
)


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_cache = {
    "data":           None,   # last successful result dict, or None
    "ts":             0.0,    # wall-clock of last fetch attempt
    "warned_failed":  False,  # one-time console warning when both sources fail
}

# Permanent geocoding cache — the geo coordinates never change at runtime.
_geo_cache: dict[str, tuple[float, float, str]] = {}


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _internet_ok(host: str = "8.8.8.8", port: int = 53, timeout: float = 1.5) -> bool:
    """Quick connectivity check: attempt a UDP connect to a well-known host."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False


def _http_get_json(url: str, timeout: float = 7.0) -> dict:
    """Fetch a URL and parse the JSON response. Returns {} on any error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AtomMan-Echo/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return {}


def _ascii(s: str) -> str:
    """Strip non-ASCII and semicolons (AtomMan protocol uses ; as delimiter)."""
    return re.sub(r"[^\x20-\x7E]", "?", s).replace(";", ",")


# ---------------------------------------------------------------------------
# Open-Meteo geocoding
# ---------------------------------------------------------------------------

def _geocode_openmeteo(location: str) -> tuple[float, float, str] | None:
    """Resolve OW_LOCATION to (lat, lon, zone_str) using Open-Meteo geocoding.

    Results are cached permanently — the location never changes at runtime.
    Supports:
      - "lat,lon"       — parsed directly, no network call
      - "city"          — geocoding API lookup
      - "city,country"  — city portion sent to geocoding API
    """
    loc = location.strip()
    if not loc:
        return None

    if loc in _geo_cache:
        return _geo_cache[loc]

    # "lat,lon" — two bare floats
    parts = [p.strip() for p in loc.split(",", 1)]
    if len(parts) == 2:
        try:
            lat, lon = float(parts[0]), float(parts[1])
            result = (lat, lon, f"{lat:.4f},{lon:.4f}")
            _geo_cache[loc] = result
            return result
        except ValueError:
            pass

    # City name (take everything before the first comma as the search term)
    city = parts[0].strip()
    q = urllib.parse.quote(city)
    j = _http_get_json(
        f"https://geocoding-api.open-meteo.com/v1/search"
        f"?name={q}&count=1&language=en&format=json"
    )

    results = j.get("results")
    if not results:
        return None

    ent    = results[0]
    lat    = float(ent.get("latitude", 0))
    lon    = float(ent.get("longitude", 0))
    name   = ent.get("name") or city
    admin1 = ent.get("admin1") or ""
    zone   = f"{name}, {admin1}" if admin1 else name

    result = (lat, lon, zone)
    _geo_cache[loc] = result
    return result


# ---------------------------------------------------------------------------
# WMO weather code helpers
# ---------------------------------------------------------------------------

def _map_wmo_to_weather_n(code: int) -> int:
    """Map a WMO weather code to an AtomMan weatherN display code.

    Always returns the daytime variant (Open-Meteo provides no day/night icon).
    """
    if code == 0:
        return 1    # Clear sky
    if code in (1, 2):
        return 5    # Mainly clear / partly cloudy
    if code == 3:
        return 9    # Overcast
    if code in (45, 48):
        return 30   # Fog / depositing rime fog
    if code in (51, 53, 55):
        return 13   # Drizzle: light, moderate, dense
    if code in (56, 57):
        return 19   # Freezing drizzle: light, heavy
    if code in (61, 63):
        return 13   # Rain: slight, moderate
    if code == 65:
        return 15   # Rain: heavy
    if code in (66, 67):
        return 19   # Freezing rain: light, heavy
    if code in (71, 73, 75, 77):
        return 22   # Snow fall / snow grains
    if code in (80, 81):
        return 10   # Rain showers: slight, moderate
    if code == 82:
        return 15   # Rain showers: violent
    if code in (85, 86):
        return 21   # Snow showers: slight, heavy
    if code == 95:
        return 11   # Thunderstorm
    if code in (96, 99):
        return 16   # Thunderstorm with hail
    return 99       # Unknown


def _wmo_code_to_desc(code: int) -> str:
    """Return a short English description for a WMO weather code."""
    _WMO_DESC = {
        0:  "Clear sky",
        1:  "Mainly clear",
        2:  "Partly cloudy",
        3:  "Overcast",
        45: "Fog",
        48: "Rime fog",
        51: "Light drizzle",
        53: "Drizzle",
        55: "Dense drizzle",
        56: "Light freezing drizzle",
        57: "Freezing drizzle",
        61: "Light rain",
        63: "Rain",
        65: "Heavy rain",
        66: "Light freezing rain",
        67: "Freezing rain",
        71: "Light snow",
        73: "Snow",
        75: "Heavy snow",
        77: "Snow grains",
        80: "Rain showers",
        81: "Rain showers",
        82: "Violent showers",
        85: "Snow showers",
        86: "Heavy snow showers",
        95: "Thunderstorm",
        96: "Thunderstorm w/ hail",
        99: "Thunderstorm w/ hail",
    }
    return _WMO_DESC.get(code, f"WMO {code}")


# ---------------------------------------------------------------------------
# Open-Meteo fetch
# ---------------------------------------------------------------------------

def _fetch_openmeteo() -> dict | None:
    """Fetch current weather from Open-Meteo (no API key required).

    Returns a dict {weatherN, lo, hi, zone, desc} on success, None otherwise.
    """
    geo = _geocode_openmeteo(OW_LOCATION)
    if not geo:
        return None

    lat, lon, zone = geo

    temp_unit = "fahrenheit" if OW_UNITS == "imperial" else "celsius"

    qs = urllib.parse.urlencode({
        "latitude":         f"{lat:.6f}",
        "longitude":        f"{lon:.6f}",
        "current":          "temperature_2m,weather_code",
        "daily":            "weather_code,temperature_2m_max,temperature_2m_min",
        "timezone":         "auto",
        "forecast_days":    "1",
        "temperature_unit": temp_unit,
    })
    j = _http_get_json(f"https://api.open-meteo.com/v1/forecast?{qs}")

    cur   = j.get("current", {})
    daily = j.get("daily", {})
    if not cur or not daily:
        return None

    code = int(cur.get("weather_code", 0) or 0)
    lo   = int(round(float((daily.get("temperature_2m_min") or [0])[0])))
    hi   = int(round(float((daily.get("temperature_2m_max") or [0])[0])))
    desc = _wmo_code_to_desc(code)

    return {
        "weatherN":     _map_wmo_to_weather_n(code),
        "lo":           lo,
        "hi":           hi,
        "zone":         _ascii(zone),   # ascii-safe for AtomMan serial protocol
        "zone_display": zone,           # unicode for dashboard display
        "desc":         _ascii(desc),
    }


# ---------------------------------------------------------------------------
# OpenWeather fetch (fallback — requires API key)
# ---------------------------------------------------------------------------

def _parse_location_ow(loc: str, key: str) -> tuple | None:
    """Parse OW_LOCATION for OpenWeather and return (lat, lon, zone_str) or None."""
    loc = loc.strip()
    if not loc or not key:
        return None

    parts = [p.strip() for p in loc.split(",", 1)]
    if len(parts) == 2:
        try:
            lat, lon = float(parts[0]), float(parts[1])
            return lat, lon, f"{lat:.4f},{lon:.4f}"
        except ValueError:
            pass

    q = urllib.parse.quote(loc)
    j = _http_get_json(
        f"https://api.openweathermap.org/geo/1.0/direct?q={q}&limit=1&appid={key}"
    )
    if isinstance(j, list) and j:
        ent   = j[0]
        name  = ent.get("name") or loc
        cc    = ent.get("country", "")
        state = ent.get("state")
        zone  = f"{name},{cc}" if cc else name
        if state and state not in zone:
            zone = f"{name}, {state}, {cc}" if cc else f"{name}, {state}"
        return float(ent["lat"]), float(ent["lon"]), zone

    return None


def _ow_id_to_weather_n(ow_id: int, icon: str) -> int:
    """Map an OpenWeather condition ID + icon to an AtomMan weatherN code."""
    day = icon.endswith("d") if icon else True

    if ow_id == 800:
        return 1 if day else 3
    if ow_id == 801:
        return 5 if day else 6
    if ow_id == 802:
        return 7 if day else 8
    if ow_id in (803, 804):
        return 9

    g = ow_id // 100
    if g == 2:
        if ow_id in (202, 212, 232):
            return 16
        return 11
    if g == 3:
        return 13
    if g == 5:
        if ow_id == 511:
            return 19
        if ow_id in (520, 521, 522, 531):
            return 10
        if ow_id == 500:
            return 13
        if ow_id == 501:
            return 14
        if ow_id in (502, 503, 504):
            return 15
        return 14
    if g == 6:
        if ow_id in (611, 612, 615, 616):
            return 20
        if ow_id == 600:
            return 22
        if ow_id == 601:
            return 23
        if ow_id in (602, 621, 622):
            return 24
        if ow_id == 620:
            return 21
        return 22
    if g == 7:
        if ow_id in (701, 741):
            return 30
        if ow_id in (711, 721):
            return 31
        if ow_id in (731, 751):
            return 27
        if ow_id in (761, 762):
            return 26
        if ow_id == 771:
            return 33
        if ow_id == 781:
            return 36
        return 31

    return 99


def _fetch_openweather() -> dict | None:
    """Fetch from OpenWeather One Call API. Requires OW_API_KEY."""
    key = (OW_API_KEY or "").strip()
    if not key:
        return None

    try:
        loc = _parse_location_ow(OW_LOCATION, key)
        if not loc:
            return None
        lat, lon, zone = loc

        qs = urllib.parse.urlencode({
            "lat":     f"{lat:.6f}",
            "lon":     f"{lon:.6f}",
            "units":   OW_UNITS,
            "lang":    OW_LANG,
            "exclude": "minutely,hourly,alerts",
            "appid":   key,
        })
        j = _http_get_json(
            f"https://api.openweathermap.org/data/3.0/onecall?{qs}"
        )
        if not j or "current" not in j or not j.get("daily"):
            return None

        cur   = j["current"]
        d0    = j["daily"][0]
        w     = cur.get("weather", [{}])[0]
        ow_id = int(w.get("id", 0) or 0)
        icon  = str(w.get("icon", "") or "")
        desc  = str(w.get("description", "") or "")

        temps = d0.get("temp", {})
        lo    = int(round(float(temps.get("min", cur.get("temp", 0)))))
        hi    = int(round(float(temps.get("max", cur.get("temp", 0)))))

        return {
            "weatherN":     _ow_id_to_weather_n(ow_id, icon),
            "lo":           lo,
            "hi":           hi,
            "zone":         _ascii(zone),   # ascii-safe for serial protocol
            "zone_display": zone,           # unicode for dashboard display
            "desc":         _ascii(desc),
        }

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Unified fetch with fallback chain
# ---------------------------------------------------------------------------

def _fetch_now() -> dict | None:
    """Fetch fresh weather data, trying Open-Meteo first then OpenWeather.

    Returns a dict {weatherN, lo, hi, zone, desc} on success, None otherwise.
    Emits a one-time warning to stdout only when both sources fail.
    """
    if not _internet_ok():
        return None

    # Primary: Open-Meteo (no API key required)
    data = _fetch_openmeteo()
    if data is not None:
        _cache["warned_failed"] = False   # reset warning if we recover
        return data

    # Fallback: OpenWeather (only if key is configured)
    key = (OW_API_KEY or "").strip()
    if key:
        data = _fetch_openweather()
        if data is not None:
            _cache["warned_failed"] = False
            return data

    # Both failed — warn once
    if not _cache["warned_failed"]:
        print("[Weather] Both Open-Meteo and OpenWeather failed — weather fields will be blank.")
        _cache["warned_failed"] = True

    return None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def get_weather_cached() -> dict | None:
    """Return cached weather data, refreshing from the API when stale.

    Refreshes at most every WEATHER_REFRESH_SECONDS seconds.
    Returns None when all sources are unavailable.
    """
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < WEATHER_REFRESH_SECONDS:
        return _cache["data"]

    data = _fetch_now()
    _cache["data"] = data
    _cache["ts"]   = now
    return data


def get_weather_age_s() -> int:
    """Return how many seconds ago the weather cache was last updated."""
    return int(time.time() - _cache["ts"])
