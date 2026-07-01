"""Pass prediction + Doppler, powered by Skyfield (the Orbitron replacement).

Given the registry and an observer location, this computes the AOS / max-
elevation / LOS times for every upcoming pass, plus per-pass geometry
(azimuths, peak elevation) and a Doppler curve for the downlink frequency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import numpy as np
from skyfield.api import EarthSatellite, load, wgs84

from .config import Config, Satellite
from .registry import RegistryEntry

log = logging.getLogger("sattrack.predict")

C_M_PER_S = 299_792_458.0

# Skyfield's timescale loads ephemeris/leap-second data once; reuse it.
_TS = load.timescale()


@dataclass
class Pass:
    satellite: str
    norad_id: int
    freq_mhz: float
    decoder: str
    aos: datetime          # UTC
    los: datetime          # UTC
    max_time: datetime     # UTC
    max_elevation_deg: float
    aos_azimuth_deg: float
    los_azimuth_deg: float
    priority: int = 1
    pipeline: Optional[str] = None
    satdump_satellite_number: Optional[str] = None
    gr_name: Optional[str] = None
    samplerate: Optional[int] = None
    mode: str = "fm"

    @property
    def duration_s(self) -> float:
        return (self.los - self.aos).total_seconds()

    @property
    def direction(self) -> str:
        """Crude N->S vs S->N hint from AOS azimuth (useful for APT framing)."""
        return "northbound" if 90 < self.aos_azimuth_deg < 270 else "southbound"

    def to_dict(self) -> dict:
        return {
            "satellite": self.satellite,
            "norad_id": self.norad_id,
            "freq_mhz": self.freq_mhz,
            "decoder": self.decoder,
            "aos": self.aos.isoformat(),
            "los": self.los.isoformat(),
            "max_time": self.max_time.isoformat(),
            "max_elevation_deg": round(self.max_elevation_deg, 1),
            "aos_azimuth_deg": round(self.aos_azimuth_deg, 1),
            "los_azimuth_deg": round(self.los_azimuth_deg, 1),
            "duration_s": round(self.duration_s),
            "direction": self.direction,
            "priority": self.priority,
        }


def _earth_satellite(entry: RegistryEntry) -> EarthSatellite:
    l1, l2 = entry.tle.lines
    return EarthSatellite(l1, l2, entry.name, _TS)


def _to_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def predict_passes_for(
    entry: RegistryEntry,
    observer_topos,
    start: datetime,
    end: datetime,
    min_elevation_deg: float,
) -> List[Pass]:
    sat = _earth_satellite(entry)
    t0 = _TS.from_datetime(_to_utc(start))
    t1 = _TS.from_datetime(_to_utc(end))

    times, events = sat.find_events(
        observer_topos, t0, t1, altitude_degrees=min_elevation_deg
    )

    difference = sat - observer_topos
    passes: List[Pass] = []
    cur: Dict[str, object] = {}

    for ti, ev in zip(times, events):
        topocentric = difference.at(ti)
        alt, az, _ = topocentric.altaz()
        when = ti.utc_datetime()

        if ev == 0:  # rise / AOS
            cur = {"aos": when, "aos_az": az.degrees}
        elif ev == 1:  # culminate / max elevation
            cur["max_time"] = when
            cur["max_el"] = alt.degrees
        elif ev == 2:  # set / LOS
            if "aos" not in cur:
                continue  # pass that began before the window; skip partial
            cur["los"] = when
            cur["los_az"] = az.degrees
            passes.append(
                Pass(
                    satellite=entry.name,
                    norad_id=entry.satellite.norad_id,
                    freq_mhz=entry.satellite.freq_mhz,
                    decoder=entry.satellite.decoder,
                    aos=cur["aos"],            # type: ignore[arg-type]
                    los=cur["los"],            # type: ignore[arg-type]
                    max_time=cur.get("max_time", cur["aos"]),  # type: ignore[arg-type]
                    max_elevation_deg=float(cur.get("max_el", min_elevation_deg)),
                    aos_azimuth_deg=float(cur["aos_az"]),       # type: ignore[arg-type]
                    los_azimuth_deg=float(cur["los_az"]),       # type: ignore[arg-type]
                    priority=entry.satellite.priority,
                    pipeline=entry.satellite.pipeline,
                    satdump_satellite_number=entry.satellite.satdump_satellite_number,
                    gr_name=entry.satellite.gr_name,
                    samplerate=entry.satellite.samplerate,
                    mode=entry.satellite.mode,
                )
            )
            cur = {}

    return passes


def pass_from_satellite(sat: Satellite) -> Pass:
    """Minimal :class:`Pass` stub for offline decode / doctor previews."""
    now = datetime.now(timezone.utc)
    return Pass(
        satellite=sat.name,
        norad_id=sat.norad_id,
        freq_mhz=sat.freq_mhz,
        decoder=sat.decoder,
        aos=now,
        los=now,
        max_time=now,
        max_elevation_deg=0.0,
        aos_azimuth_deg=0.0,
        los_azimuth_deg=0.0,
        priority=sat.priority,
        pipeline=sat.pipeline,
        satdump_satellite_number=sat.satdump_satellite_number,
        gr_name=sat.gr_name,
        samplerate=sat.samplerate,
        mode=sat.mode,
    )


def predict_all(
    config: Config,
    registry: Dict[str, RegistryEntry],
    start: Optional[datetime] = None,
    hours: Optional[float] = None,
) -> List[Pass]:
    """Predict every pass for every registry satellite, sorted by AOS."""
    obs = config.observer
    topos = wgs84.latlon(obs.latitude, obs.longitude, elevation_m=obs.elevation_m)

    start = start or datetime.now(timezone.utc)
    hours = hours if hours is not None else config.prediction.horizon_hours
    end = start + timedelta(hours=hours)

    all_passes: List[Pass] = []
    for entry in registry.values():
        try:
            all_passes.extend(
                predict_passes_for(
                    entry, topos, start, end, config.prediction.min_elevation_deg
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.error("prediction failed for %s: %s", entry.name, exc)

    all_passes.sort(key=lambda p: p.aos)
    log.info("predicted %d passes in next %.0fh", len(all_passes), hours)
    return all_passes


def doppler_curve(
    config: Config,
    entry: RegistryEntry,
    sat_pass: Pass,
    samples: int = 30,
) -> List[dict]:
    """Sample the Doppler shift (Hz) of the downlink across the pass.

    Returns a list of ``{t, elevation_deg, range_km, range_rate_km_s,
    doppler_hz, corrected_freq_hz}``.
    """
    obs = config.observer
    topos = wgs84.latlon(obs.latitude, obs.longitude, elevation_m=obs.elevation_m)
    sat = _earth_satellite(entry)
    diff = sat - topos
    freq_hz = entry.freq_hz

    span = (sat_pass.los - sat_pass.aos).total_seconds()
    out: List[dict] = []
    for i in range(samples + 1):
        when = sat_pass.aos + timedelta(seconds=span * i / samples)
        ti = _TS.from_datetime(_to_utc(when))
        topo = diff.at(ti)
        alt, _, dist = topo.altaz()

        r = topo.position.km
        v = topo.velocity.km_per_s
        rng = float(np.linalg.norm(r))
        range_rate = float(np.dot(r, v) / rng)  # km/s, +ve = receding
        doppler = -range_rate * 1000.0 / C_M_PER_S * freq_hz

        out.append(
            {
                "t": when.isoformat(),
                "elevation_deg": round(alt.degrees, 2),
                "range_km": round(rng, 1),
                "range_rate_km_s": round(range_rate, 4),
                "doppler_hz": round(doppler, 1),
                "corrected_freq_hz": round(freq_hz - doppler, 1),
            }
        )
    return out


def next_pass(passes: List[Pass], after: Optional[datetime] = None) -> Optional[Pass]:
    after = after or datetime.now(timezone.utc)
    upcoming = [p for p in passes if p.los > after]
    return upcoming[0] if upcoming else None
