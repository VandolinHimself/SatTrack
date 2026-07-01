"""NORAD / Celestrak TLE ingestion.

A cross-platform, importable evolution of the original ``tle_gen.py``:

* fetches GP/TLE data for a list of NORAD catalog IDs from Celestrak
* caches by content hash so we only log "real" updates
* persists TLEs locally so prediction still works if Celestrak is unreachable
* stores everything under a platform-appropriate data directory

The public entry point is :func:`update_tles`, which returns a
``{norad_id: TLE}`` map ready for the registry layer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests

log = logging.getLogger("sattrack.tle")

NORAD_URL = "https://celestrak.org/NORAD/elements/gp.php"
USER_AGENT = "SatTrack/3.0 (+https://celestrak.org)"


def data_dir() -> Path:
    """Cross-platform writable data directory for caches + TLEs."""
    env = os.environ.get("SATTRACK_DATA")
    if env:
        base = Path(env)
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SatTrack"
    else:
        # Prefer the FHS path if writable (matches the original script), else ~/.
        fhs = Path("/var/lib/sattracker")
        try:
            fhs.mkdir(parents=True, exist_ok=True)
            base = fhs
        except PermissionError:
            base = Path.home() / ".local" / "share" / "sattrack"
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class TLE:
    norad_id: int
    name: str
    line1: str
    line2: str
    fetched_at: str  # ISO8601 UTC

    @property
    def lines(self) -> List[str]:
        return [self.line1, self.line2]

    @property
    def epoch(self) -> Optional[datetime]:
        """Decode the TLE epoch (cols 19-32 of line 1) into a UTC datetime."""
        try:
            field = self.line1[18:32].strip()
            yy = int(field[0:2])
            day = float(field[2:])
            year = 2000 + yy if yy < 57 else 1900 + yy
            base = datetime(year, 1, 1, tzinfo=timezone.utc)
            from datetime import timedelta

            return base + timedelta(days=day - 1)
        except Exception:
            return None

    @property
    def age_hours(self) -> Optional[float]:
        ep = self.epoch
        if ep is None:
            return None
        return (datetime.now(timezone.utc) - ep).total_seconds() / 3600.0


def _cache_paths(cat: int) -> tuple[Path, Path]:
    d = data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{cat}.hash", d / f"{cat}.tle"


def _hash(lines: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _download(cat: int, timeout: float = 15.0) -> Optional[TLE]:
    url = f"{NORAD_URL}?CATNR={cat}&FORMAT=TLE"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - network errors are expected/loggable
        log.warning("fetch failed for %s: %s", cat, exc)
        return None

    lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
    # Celestrak returns "No GP data found" as a single line on bad IDs.
    if len(lines) < 3 or not lines[1].startswith("1 ") or not lines[2].startswith("2 "):
        log.warning("no valid TLE for %s (got %d lines)", cat, len(lines))
        return None

    return TLE(
        norad_id=cat,
        name=lines[0],
        line1=lines[1],
        line2=lines[2],
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _load_cached_tle(cat: int) -> Optional[TLE]:
    _, tle_path = _cache_paths(cat)
    if not tle_path.exists():
        return None
    try:
        return TLE(**json.loads(tle_path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _save_cached_tle(tle: TLE) -> None:
    hash_path, tle_path = _cache_paths(tle.norad_id)
    tle_path.write_text(json.dumps(asdict(tle)), encoding="utf-8")
    hash_path.write_text(_hash(tle.lines), encoding="utf-8")


def _fetch_one(cat: int) -> Optional[TLE]:
    fresh = _download(cat)
    if fresh is None:
        cached = _load_cached_tle(cat)
        if cached is not None:
            log.info("using cached TLE for %s (age %.1fh)", cat, cached.age_hours or -1)
        return cached

    hash_path, _ = _cache_paths(cat)
    old_hash = hash_path.read_text(encoding="utf-8").strip() if hash_path.exists() else None
    if old_hash != _hash(fresh.lines):
        log.info("updated TLE: %s (%s)", cat, fresh.name)
    _save_cached_tle(fresh)
    return fresh


def update_tles(norad_ids: Iterable[int], max_workers: int = 8) -> Dict[int, TLE]:
    """Fetch (or fall back to cached) TLEs for ``norad_ids``.

    Returns a ``{norad_id: TLE}`` dict. IDs that have neither a fresh nor a
    cached TLE are omitted (and warned about).
    """
    ids = list(dict.fromkeys(int(x) for x in norad_ids))  # de-dupe, keep order
    out: Dict[int, TLE] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for cat, tle in zip(ids, ex.map(_fetch_one, ids)):
            if tle is not None:
                out[cat] = tle
            else:
                log.error("no TLE available for %s", cat)

    _write_combined_tle(out.values())
    _stamp_update()
    return out


def _write_combined_tle(tles: Iterable[TLE]) -> None:
    out_file = data_dir() / "custom.tle"
    with open(out_file, "w", encoding="utf-8", newline="\r\n") as f:
        for tle in tles:
            f.write(f"{tle.name}\n{tle.line1}\n{tle.line2}\n")


def _stamp_update() -> None:
    stamp = data_dir() / "last_update.txt"
    stamp.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")


def read_satellites_file(file_path: str | os.PathLike) -> List[int]:
    """Parse a plain ``satellites.txt`` of NORAD IDs (compat with tle_gen.py)."""
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    ids: List[int] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line.isdigit():
            ids.append(int(line))
    return ids
