"""Live orbital geometry for the web dashboard.

Computes sub-satellite points, observer look angles, and ground-track polylines
using the same Skyfield/TLE stack as pass prediction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from skyfield.api import wgs84

from .config import Config
from .predict import Pass, _TS, _earth_satellite, _to_utc, doppler_curve, predict_all
from .registry import RegistryEntry, build_registry
from .watcher import _capture_window, _key, next_display_pass, plan_schedule

# Distinct hues per NORAD (stable across refreshes).
_SAT_COLORS = {
    25544: "#22d3ee",  # ISS — cyan
    57166: "#fb923c",  # METEOR — orange
    43017: "#4ade80",  # AO-91 — green
    27607: "#c084fc",  # SO-50 — violet
    39444: "#f472b6",  # AO-73 — pink
}


def _color(norad_id: int) -> str:
    return _SAT_COLORS.get(norad_id, "#94a3b8")


def _observer_topos(config: Config):
    obs = config.observer
    return wgs84.latlon(obs.latitude, obs.longitude, elevation_m=obs.elevation_m)


def satellite_now(entry: RegistryEntry, config: Config, at: Optional[datetime] = None) -> dict:
    """Current position + observer geometry for one satellite."""
    at = _to_utc(at or datetime.now(timezone.utc))
    t = _TS.from_datetime(at)
    sat = _earth_satellite(entry)
    topos = _observer_topos(config)
    diff = sat - topos
    topo = diff.at(t)
    alt, az, dist = topo.altaz()
    sub = wgs84.subpoint_of(sat.at(t))
    pos = sat.at(t).position.km
    vel = sat.at(t).velocity.km_per_s
    speed = float((vel[0] ** 2 + vel[1] ** 2 + vel[2] ** 2) ** 0.5)
    s = entry.satellite
    return {
        "name": entry.name,
        "norad_id": s.norad_id,
        "freq_mhz": s.freq_mhz,
        "decoder": s.decoder,
        "color": _color(s.norad_id),
        "lat": round(float(sub.latitude.degrees), 4),
        "lon": round(float(sub.longitude.degrees), 4),
        "alt_km": round(float(sub.elevation.km), 1),
        "elevation_deg": round(float(alt.degrees), 2),
        "azimuth_deg": round(float(az.degrees), 2),
        "range_km": round(float(dist.km), 1),
        "speed_km_s": round(speed, 2),
        "visible": float(alt.degrees) > 0,
        "timestamp": at.isoformat(),
    }


def ground_track(
    entry: RegistryEntry,
    *,
    minutes_past: float = 30.0,
    minutes_future: float = 90.0,
    step_seconds: float = 90.0,
    at: Optional[datetime] = None,
) -> List[dict]:
    """Sub-satellite polyline for map/globe rendering."""
    center = _to_utc(at or datetime.now(timezone.utc))
    sat = _earth_satellite(entry)
    points: List[dict] = []
    start = center - timedelta(minutes=minutes_past)
    end = center + timedelta(minutes=minutes_future)
    step = timedelta(seconds=step_seconds)
    t_cur = start
    while t_cur <= end:
        ts = _TS.from_datetime(t_cur)
        sub = wgs84.subpoint_of(sat.at(ts))
        points.append({
            "t": t_cur.isoformat(),
            "lat": round(float(sub.latitude.degrees), 4),
            "lon": round(float(sub.longitude.degrees), 4),
            "alt_km": round(float(sub.elevation.km), 1),
        })
        t_cur += step
    return points


def build_tracks(registry: Dict[str, RegistryEntry]) -> Dict[str, list]:
    """Ground-track polylines — expensive; cache on the server."""
    return {name: ground_track(entry) for name, entry in registry.items()}


def build_schedule_snapshot(config: Config, registry: Optional[Dict[str, RegistryEntry]] = None) -> dict:
    """Pass schedule — changes slowly; cache separately from live positions."""
    registry = registry or build_registry(config, refresh=False)
    now = datetime.now(timezone.utc)
    passes = predict_all(config, registry)
    planned, skipped = plan_schedule(passes, config)
    planned_keys = {_key(p) for p in planned}
    skip_keys = skipped

    schedule = []
    for p in passes:
        if p.los <= now:
            continue
        pk = _key(p)
        if pk in planned_keys:
            w0, w1 = _capture_window(p, config)
            status = "scheduled"
            window = f"{w0.astimezone().strftime('%H:%M')}-{w1.astimezone().strftime('%H:%M')}"
        elif pk in skip_keys:
            status = "skipped"
            window = ""
        else:
            status = "past"
            window = ""
        schedule.append({**p.to_dict(), "status": status, "window": window})

    schedule.sort(key=lambda x: x["aos"])
    next_pass = next_display_pass(passes, config, now)
    return {
        "schedule": schedule[:24],
        "next_pass": next_pass.to_dict() if next_pass else None,
    }


def build_live_scene(
    config: Config,
    registry: Optional[Dict[str, RegistryEntry]] = None,
    *,
    tracks: Optional[Dict[str, list]] = None,
    schedule: Optional[list] = None,
    next_pass: Optional[dict] = None,
    include_tracks: bool = True,
    include_schedule: bool = True,
) -> dict:
    """Live snapshot: observer + satellite positions (+ optional cached tracks/schedule)."""
    registry = registry or build_registry(config, refresh=False)
    obs = config.observer
    now = datetime.now(timezone.utc)

    satellites = [satellite_now(entry, config, now) for entry in registry.values()]

    if include_tracks and tracks is None:
        tracks = build_tracks(registry)
    if include_schedule and schedule is None:
        snap = build_schedule_snapshot(config, registry)
        schedule = snap["schedule"]
        next_pass = snap["next_pass"]

    return {
        "timestamp": now.isoformat(),
        "observer": {
            "name": obs.name,
            "lat": obs.latitude,
            "lon": obs.longitude,
            "elev_m": obs.elevation_m,
        },
        "satellites": satellites,
        "tracks": tracks or {},
        "schedule": schedule or [],
        "next_pass": next_pass,
    }


def pass_detail(config: Config, entry: RegistryEntry, sat_pass: Pass, samples: int = 40) -> dict:
    """Rich pass card: geometry + Doppler samples for the dashboard."""
    curve = doppler_curve(config, entry, sat_pass, samples=samples)
    return {
        "pass": sat_pass.to_dict(),
        "doppler": curve,
        "satellite": {
            "name": entry.name,
            "norad_id": entry.satellite.norad_id,
            "freq_mhz": entry.satellite.freq_mhz,
            "decoder": entry.satellite.decoder,
            "mode": entry.satellite.mode,
            "pipeline": entry.satellite.pipeline,
            "color": _color(entry.satellite.norad_id),
        },
    }
