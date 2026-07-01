"""Configuration loading for SatTrack.

The whole stack is driven by a single ``config.json`` (observer location,
prediction window, capture/SDR settings, and the satellite registry). This
module turns that file into typed dataclasses with sensible defaults so the
rest of the code never has to poke at raw dicts.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


@dataclass
class Observer:
    name: str = "Richardson, TX"
    latitude: float = 32.985
    longitude: float = -96.709
    elevation_m: float = 180.0


@dataclass
class Prediction:
    min_elevation_deg: float = 10.0
    horizon_hours: float = 24.0
    tle_max_age_hours: float = 12.0
    # How often to re-fetch TLEs from Celestrak while the daemon is running.
    tle_refresh_interval_hours: float = 4.0


@dataclass
class WatcherCfg:
    # How often (seconds) the daemon prints a "still working" status line.
    heartbeat_seconds: float = 5.0
    # Don't start a pass if less than this many seconds remain until LOS.
    min_remaining_seconds: float = 120.0


@dataclass
class KismetCfg:
    """Kismet ADS-B flight map — same ``/phy/ADSB/map_data`` feed as the GUI tab."""
    enabled: bool = True
    url: str = "http://10.0.10.121:2501"
    username: str = "lucius"
    password: str = ""
    api_key: str = ""
    password_file: str = ""
    map_data_path: str = ""
    timeout_seconds: float = 4.0


@dataclass
class SdrSharing:
    """Coordinate exclusive RTL-SDR access with another consumer (e.g. kismet).

    Before a capture the daemon runs ``release_command`` to free the dongle,
    waits ``settle_seconds``, captures, then runs ``reacquire_command`` to hand
    the SDR back — even if the capture errors out.

    With ``watchdog`` enabled the daemon also makes sure the other consumer is
    running whenever SatTrack isn't capturing: it checks ``status_command``
    (exit 0 == up) every ``watchdog_interval_seconds`` and re-runs
    ``reacquire_command`` if it's down.
    """
    enabled: bool = False
    release_command: str = ""
    reacquire_command: str = ""
    settle_seconds: float = 3.0
    watchdog: bool = False
    status_command: str = ""
    watchdog_interval_seconds: float = 30.0


@dataclass
class Capture:
    # backend: "satdump_live" (SatDump tunes the SDR + decodes in one shot,
    # the recommended prod path), "rtl_fm" (rtl_fm|sox -> wav -> aptdec), or
    # None to auto-pick (satdump_live if satdump is installed, else rtl_fm).
    backend: Optional[str] = None
    sample_rate: int = 60000        # rtl_fm audio bandwidth (rtl_fm backend)
    live_sample_rate: int = 250000  # SDR sample rate for satdump_live (RTL-valid)
    audio_rate: int = 11025
    gain: float = 45.0
    ppm: int = 0
    bias_tee: bool = False
    pre_aos_seconds: int = 60
    post_los_seconds: int = 30
    post_pass_cooldown_seconds: int = 20  # USB/SDR settle after each capture
    post_handoff_settle_seconds: int = 5   # extra settle after kismet restarts
    device_index: int = 0
    output_dir: str = "captures"
    decode: bool = True
    # satdump_mode: "auto" (offline on SatDump v1.x, live on v2+), "live", "offline"
    satdump_mode: str = "auto"
    # dry_run None => auto-detect (True on non-Linux or when no SDR tooling).
    dry_run: Optional[bool] = None


@dataclass
class Satellite:
    norad_id: int
    name: str
    freq_mhz: float
    # decoder/handler: how we turn RF into data.
    #   "satdump"  -> SatDump live pipeline (telemetry/imagery); needs `pipeline`
    #   "noaa_apt" -> SatDump live noaa_apt (legacy weather)
    #   "aprs"     -> NBFM -> AX.25 packet decode (direwolf/atest)
    #   "sstv"     -> NBFM audio -> SSTV image (sstv/qsstv) or archive
    #   "gr_satellites" -> gr-satellites amateur telemetry decoder
    #   "fm"       -> NBFM voice/beacon audio archive (no decode)
    decoder: str = "satdump"
    pipeline: Optional[str] = None      # SatDump pipeline id (for satdump/noaa_apt)
    satdump_satellite_number: Optional[str] = None  # SatDump id, e.g. "M2-3" for Meteor-M2 3
    gr_name: Optional[str] = None       # gr-satellites identifier, e.g. "AO-91", "FUNcube-1"
    samplerate: Optional[int] = None    # SDR sample-rate override (sps) for this target
    # mode: capture mode for audio/IQ backends:
    #   "fm"  -> NBFM demod audio (DUV/AFSK/voice)
    #   "wfm" -> wideband FM audio
    #   "iq"  -> raw IQ baseband (BPSK/PSK telemetry, e.g. FUNcube) via rtl_sdr
    mode: str = "fm"
    priority: int = 1
    enabled: bool = True


@dataclass
class Config:
    observer: Observer = field(default_factory=Observer)
    prediction: Prediction = field(default_factory=Prediction)
    capture: Capture = field(default_factory=Capture)
    watcher: WatcherCfg = field(default_factory=WatcherCfg)
    sdr_sharing: SdrSharing = field(default_factory=SdrSharing)
    kismet: KismetCfg = field(default_factory=KismetCfg)
    satellites: List[Satellite] = field(default_factory=list)
    path: Optional[Path] = None

    @property
    def enabled_satellites(self) -> List[Satellite]:
        return [s for s in self.satellites if s.enabled]

    @property
    def norad_ids(self) -> List[int]:
        return [s.norad_id for s in self.enabled_satellites]

    def satellite_by_name(self, name: str) -> Optional[Satellite]:
        for s in self.satellites:
            if s.name == name:
                return s
        return None

    def resolve_output_dir(self) -> Path:
        base = Path(self.capture.output_dir)
        if not base.is_absolute() and self.path is not None:
            base = self.path.parent / base
        base.mkdir(parents=True, exist_ok=True)
        return base


def _coerce(cls, data: dict):
    """Build a dataclass, ignoring unknown keys so configs stay forward-compatible."""
    fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in fields})


def load_config(path: Optional[os.PathLike | str] = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Config not found: {cfg_path}. Copy config.json and edit your location."
        )

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    sats = [_coerce(Satellite, s) for s in raw.get("satellites", [])]

    return Config(
        observer=_coerce(Observer, raw.get("observer", {})),
        prediction=_coerce(Prediction, raw.get("prediction", {})),
        capture=_coerce(Capture, raw.get("capture", {})),
        watcher=_coerce(WatcherCfg, raw.get("watcher", {})),
        sdr_sharing=_coerce(SdrSharing, raw.get("sdr_sharing", {})),
        kismet=_coerce(KismetCfg, raw.get("kismet", {})),
        satellites=sats,
        path=cfg_path,
    )


def is_linux() -> bool:
    return platform.system().lower() == "linux"
