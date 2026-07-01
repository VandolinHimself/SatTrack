"""SatTrack — automated satellite ground-station / SSA toolkit.

A small ground-station automation stack:

    config   -> observer location + satellite registry
    tle      -> NORAD/Celestrak TLE ingestion (cross-platform)
    registry -> authoritative {name: {tle, freq}} map
    predict  -> Skyfield AOS/max/LOS pass prediction + Doppler
    capture  -> event-driven RTL-SDR recording + decode pipeline
    telemetry-> SQLite SSA layer (passes, Doppler, SNR, scoring)
    watcher  -> the daemon loop that ties it all together
"""

__author__ = "Van Graham"
__version__ = "3.0"

__all__ = [
    "config",
    "tle",
    "registry",
    "predict",
    "capture",
    "telemetry",
    "watcher",
]
