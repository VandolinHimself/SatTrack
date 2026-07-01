"""Authoritative satellite registry.

Joins the operator-curated config (``Satellite``: name, frequency, decoder)
with freshly-ingested orbital data (``TLE``) to produce the single source of
truth the rest of the pipeline consumes:

    {
      "NOAA 18": {
        "norad_id": 28654,
        "freq_mhz": 137.9125,
        "decoder": "noaa_apt",
        "tle": ["line1", "line2"]
      },
      ...
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Dict, List

from .config import Config, Satellite
from .tle import TLE, update_tles

log = logging.getLogger("sattrack.registry")


@dataclass
class RegistryEntry:
    satellite: Satellite
    tle: TLE

    @property
    def name(self) -> str:
        return self.satellite.name

    @property
    def freq_hz(self) -> float:
        return self.satellite.freq_mhz * 1e6

    def as_dict(self) -> dict:
        return {
            "norad_id": self.satellite.norad_id,
            "freq_mhz": self.satellite.freq_mhz,
            "decoder": self.satellite.decoder,
            "priority": self.satellite.priority,
            "tle": self.tle.lines,
            "tle_age_hours": round(self.tle.age_hours, 2) if self.tle.age_hours else None,
        }


def build_registry(config: Config, refresh: bool = True) -> Dict[str, RegistryEntry]:
    """Build the ``{name: RegistryEntry}`` map.

    ``refresh=True`` hits Celestrak (falling back to cache); ``False`` uses
    cached TLEs only (offline/dry planning).
    """
    sats = config.enabled_satellites
    tles = update_tles([s.norad_id for s in sats]) if refresh else _cached_only(sats)

    registry: Dict[str, RegistryEntry] = {}
    max_age = config.prediction.tle_max_age_hours
    for sat in sats:
        tle = tles.get(sat.norad_id)
        if tle is None:
            log.warning("skipping %s (%s): no TLE", sat.name, sat.norad_id)
            continue
        if tle.age_hours is not None and tle.age_hours > max_age:
            log.debug(
                "%s TLE epoch is %.1fh old (limit %.1fh) — best available from Celestrak",
                sat.name, tle.age_hours, max_age,
            )
        registry[sat.name] = RegistryEntry(satellite=sat, tle=tle)

    log.info("registry built: %d/%d satellites have TLEs", len(registry), len(sats))
    return registry


def registry_has_stale_tles(registry: Dict[str, RegistryEntry], max_age_hours: float) -> bool:
    """True when any registry TLE epoch exceeds ``max_age_hours``."""
    for entry in registry.values():
        age = entry.tle.age_hours
        if age is not None and age > max_age_hours:
            return True
    return False


def _cached_only(sats: List[Satellite]) -> Dict[int, TLE]:
    from .tle import _load_cached_tle  # local import: internal helper

    out: Dict[int, TLE] = {}
    for s in sats:
        tle = _load_cached_tle(s.norad_id)
        if tle is not None:
            out[s.norad_id] = tle
    return out


def registry_to_json(registry: Dict[str, RegistryEntry], indent: int = 2) -> str:
    return json.dumps({name: e.as_dict() for name, e in registry.items()}, indent=indent)
