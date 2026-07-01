#!/usr/bin/env python3
"""Interactive SatTrack station setup — writes config.json from user prompts."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

# --- terminal styling -------------------------------------------------------

def _supports_color() -> bool:
    return sys.stdout.isatty()


def c(code: str, text: str) -> str:
    if not _supports_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str:
    return c("1", t)


def cyan(t: str) -> str:
    return c("36", t)


def yellow(t: str) -> str:
    return c("33", t)


def dim(t: str) -> str:
    return c("2", t)


def green(t: str) -> str:
    return c("32", t)


# --- prompts ----------------------------------------------------------------

def ask(prompt: str, default: str = "") -> str:
    suffix = f" {dim(f'[{default}]')}" if default else ""
    while True:
        try:
            raw = input(f"{cyan('?')} {prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit("\nSetup cancelled.")
        if raw:
            return raw
        if default:
            return default
        print(yellow("  (press Enter for default or type a value)"))


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = ask(f"{prompt} ({hint})", "y" if default else "n").lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print(yellow("  Please answer y or n."))


def ask_float(prompt: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    while True:
        raw = ask(prompt, str(default))
        try:
            val = float(raw)
        except ValueError:
            print(yellow("  Enter a number."))
            continue
        if lo is not None and val < lo:
            print(yellow(f"  Must be >= {lo}."))
            continue
        if hi is not None and val > hi:
            print(yellow(f"  Must be <= {hi}."))
            continue
        return val


def ask_int(prompt: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    while True:
        raw = ask(prompt, str(default))
        try:
            val = int(raw)
        except ValueError:
            print(yellow("  Enter a whole number."))
            continue
        if lo is not None and val < lo:
            print(yellow(f"  Must be >= {lo}."))
            continue
        if hi is not None and val > hi:
            print(yellow(f"  Must be <= {hi}."))
            continue
        return val


def try_ip_geolocation() -> tuple[float, float, str] | None:
    """Best-effort location from public IP (city-level accuracy)."""
    endpoints = (
        "https://ipinfo.io/json",
        "http://ip-api.com/json/?fields=status,lat,lon,city,regionName,country",
    )
    for url in endpoints:
        try:
            with urllib.request.urlopen(url, timeout=4) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
        if "loc" in data:
            lat_s, lon_s = data["loc"].split(",", 1)
            name = ", ".join(x for x in (data.get("city"), data.get("region"), data.get("country")) if x)
            return float(lat_s), float(lon_s), name or "My Station"
        if data.get("status") == "success":
            name = ", ".join(
                x for x in (data.get("city"), data.get("regionName"), data.get("country")) if x
            )
            return float(data["lat"]), float(data["lon"]), name or "My Station"
    return None


# --- satellite presets ------------------------------------------------------

SATELLITE_PRESETS: list[dict[str, Any]] = [
    {
        "key": "iss_aprs",
        "label": "ISS (APRS/packet @ 145.825 MHz)",
        "default": True,
        "entry": {
            "norad_id": 25544,
            "name": "ISS (APRS)",
            "freq_mhz": 145.825,
            "decoder": "aprs",
            "mode": "fm",
            "priority": 5,
        },
    },
    {
        "key": "iss_sstv",
        "label": "ISS (SSTV/voice @ 145.800 MHz)",
        "default": False,
        "entry": {
            "norad_id": 25544,
            "name": "ISS (SSTV)",
            "freq_mhz": 145.800,
            "decoder": "sstv",
            "mode": "fm",
            "priority": 5,
            "enabled": False,
        },
    },
    {
        "key": "meteor",
        "label": "METEOR-M2 3 (LRPT weather imagery @ 137.9 MHz)",
        "default": True,
        "entry": {
            "norad_id": 57166,
            "name": "METEOR-M2 3",
            "freq_mhz": 137.9,
            "decoder": "satdump",
            "pipeline": "meteor_m2-x_lrpt",
            "satdump_satellite_number": "M2-3",
            "samplerate": 1024000,
            "priority": 4,
        },
    },
    {
        "key": "ao91",
        "label": "AO-91 (FM voice/repeater @ 145.96 MHz)",
        "default": True,
        "entry": {
            "norad_id": 43017,
            "name": "AO-91",
            "freq_mhz": 145.96,
            "decoder": "fm",
            "mode": "fm",
            "priority": 2,
        },
    },
    {
        "key": "ao73",
        "label": "AO-73 FUNcube (BPSK telemetry @ 145.935 MHz — needs gr-satellites)",
        "default": False,
        "entry": {
            "norad_id": 39444,
            "name": "AO-73 FUNcube",
            "freq_mhz": 145.935,
            "decoder": "gr_satellites",
            "gr_name": "FUNcube-1",
            "mode": "iq",
            "samplerate": 250000,
            "priority": 3,
            "enabled": False,
        },
    },
    {
        "key": "so50",
        "label": "SO-50 (FM voice @ 436.795 MHz — needs a good UHF antenna)",
        "default": True,
        "entry": {
            "norad_id": 27607,
            "name": "SO-50",
            "freq_mhz": 436.795,
            "decoder": "fm",
            "mode": "fm",
            "priority": 1,
        },
    },
]


def load_template(repo_dir: Path) -> dict[str, Any]:
    for name in ("config.example.json", "config.json"):
        path = repo_dir / name
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError("No config.example.json or config.json found in repo root.")


def build_config_interactive(repo_dir: Path, *, enable_kismet: bool) -> dict[str, Any]:
    print()
    print(bold("── Station setup ─────────────────────────────────────────"))
    print(dim("  We'll write config.json for you. Ctrl-C to abort anytime."))
    print()

    cfg = deepcopy(load_template(repo_dir))

    lat, lon = 33.021746, -96.730463
    station_name = "My Station"

    if ask_yes_no("Try to detect your location from your public IP?", default=False):
        print(dim("  Looking up approximate location ..."))
        geo = try_ip_geolocation()
        if geo:
            lat, lon, station_name = geo
            print(green(f"  Found: {station_name} ({lat:.4f}, {lon:.4f})"))
        else:
            print(yellow("  Could not detect location — you'll enter coordinates manually."))

    station_name = ask("Station name (shown on the dashboard map)", station_name)
    lat = ask_float("Latitude (decimal degrees, north positive)", lat, -90, 90)
    lon = ask_float("Longitude (decimal degrees, east positive)", lon, -180, 180)
    elev = ask_float("Elevation above sea level (meters)", 180.0, -500, 9000)
    min_el = ask_float("Minimum pass elevation to record (degrees above horizon)", 5.0, 1, 89)
    gain = ask_float("RTL-SDR gain (typical 40–49 for NOAA/Meteor)", 45.0, 0, 100)
    ppm = ask_int("Frequency correction PPM (0 if unknown)", 0, -500, 500)

    print()
    print(bold("── Satellites to track ─────────────────────────────────"))
    print(dim("  Toggle which birds SatTrack schedules automatically."))
    print()

    satellites: list[dict[str, Any]] = []
    for preset in SATELLITE_PRESETS:
        on = ask_yes_no(preset["label"], default=preset["default"])
        entry = deepcopy(preset["entry"])
        entry["enabled"] = on
        satellites.append(entry)

    if not any(s.get("enabled", True) for s in satellites):
        print(yellow("  No satellites selected — enabling ISS (APRS) and METEOR-M2 3."))
        for s in satellites:
            if s["name"] in ("ISS (APRS)", "METEOR-M2 3"):
                s["enabled"] = True

    print()
    print(bold("── Kismet / ADS-B ──────────────────────────────────────"))

    kismet_on = enable_kismet and ask_yes_no(
        "Enable Kismet ADS-B on this machine? (aircraft map + SDR handoff)", default=True
    )

    kismet_url = "http://127.0.0.1:2501"
    kismet_user = ""
    kismet_pass = ""
    if kismet_on:
        if ask_yes_no("Is Kismet running on this same machine?", default=True):
            kismet_url = "http://127.0.0.1:2501"
        else:
            kismet_url = ask("Kismet URL", "http://127.0.0.1:2501")
            if not re.match(r"^https?://", kismet_url):
                kismet_url = f"http://{kismet_url}"
            kismet_user = ask("Kismet username (leave blank if localhost-only)", "")
            if kismet_user:
                kismet_pass = ask("Kismet password", "")

    cfg["observer"] = {
        "name": station_name,
        "latitude": lat,
        "longitude": lon,
        "elevation_m": elev,
    }
    cfg["prediction"]["min_elevation_deg"] = min_el
    cfg["capture"]["gain"] = gain
    cfg["capture"]["ppm"] = ppm
    cfg["satellites"] = satellites

    cfg["kismet"] = {
        "enabled": kismet_on,
        "url": kismet_url,
        "username": kismet_user,
        "password": kismet_pass,
        "api_key": "",
        "password_file": "",
        "map_data_path": "/phy/ADSB/map_data.json",
        "timeout_seconds": 4,
    }
    cfg["sdr_sharing"] = {
        "enabled": kismet_on,
        "release_command": "systemctl stop kismet",
        "reacquire_command": "systemctl start kismet",
        "settle_seconds": 3,
        "watchdog": kismet_on,
        "status_command": "systemctl is-active --quiet kismet",
        "watchdog_interval_seconds": 30,
    }

    return cfg


def build_config_defaults(repo_dir: Path, *, enable_kismet: bool) -> dict[str, Any]:
    cfg = deepcopy(load_template(repo_dir))
    if not enable_kismet:
        cfg["kismet"]["enabled"] = False
        cfg["sdr_sharing"]["enabled"] = False
        cfg["sdr_sharing"]["watchdog"] = False
    return cfg


def write_config(path: Path, cfg: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def summarize(cfg: dict[str, Any]) -> None:
    obs = cfg["observer"]
    sats = [s["name"] for s in cfg.get("satellites", []) if s.get("enabled", True)]
    print()
    print(bold("── Configuration summary ───────────────────────────────"))
    print(f"  Station   : {obs['name']}")
    print(f"  Location  : {obs['latitude']:.5f}, {obs['longitude']:.5f}  ({obs['elevation_m']:.0f} m)")
    print(f"  Min elev  : {cfg['prediction']['min_elevation_deg']}°")
    print(f"  Gain/PPM  : {cfg['capture']['gain']} / {cfg['capture']['ppm']}")
    print(f"  Kismet    : {'on' if cfg['kismet']['enabled'] else 'off'}  {cfg['kismet']['url']}")
    print(f"  Satellites: {', '.join(sats) if sats else '(none)'}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="SatTrack interactive config wizard")
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Output config path (default: repo root config.json)",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="SatTrack repo root (default: parent of scripts/)",
    )
    parser.add_argument(
        "--defaults",
        action="store_true",
        help="Non-interactive — write template defaults without prompts",
    )
    parser.add_argument(
        "--skip-kismet",
        action="store_true",
        help="Disable Kismet / sdr_sharing in generated config",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing config without asking",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    repo_dir = args.repo or script_dir.parent
    out_path = args.output or (repo_dir / "config.json")

    if out_path.exists() and not args.force and not args.defaults:
        print(yellow(f"config.json already exists at {out_path}"))
        choice = ask("Overwrite with new setup? (y/N)", "n").lower()
        if choice not in ("y", "yes"):
            print(dim("Keeping existing config.json"))
            return 0

    enable_kismet = not args.skip_kismet

    if args.defaults:
        cfg = build_config_defaults(repo_dir, enable_kismet=enable_kismet)
    else:
        cfg = build_config_interactive(repo_dir, enable_kismet=enable_kismet)

    write_config(out_path, cfg)
    summarize(cfg)
    print(green(f"Wrote {out_path}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
