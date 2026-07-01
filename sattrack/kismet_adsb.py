"""Kismet ADS-B flight map integration.

Polls the same ``/phy/ADSB/map_data`` endpoint Kismet's built-in flight map uses
and normalizes aircraft positions for the SatTrack Cesium dashboard.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from requests import Session

from .capture import consumer_is_up
from .config import Config, KismetCfg

log = logging.getLogger("sattrack.kismet")

MAP_SUFFIXES = (
    "/phy/ADSB/map_data.json",
    "/phy/ADSB/map_data",
)

# Reuse login session + cookies between polls (direct :2501 requires auth).
_HTTP: Optional[Session] = None
_HTTP_KEY: tuple = ()
_AUTH_WARN_TS: float = 0.0

AUTH_HINT = (
    "Kismet returned 401 on port 2501 — set kismet.password, kismet.api_key, "
    "KISMET_PASSWORD / KISMET_API_KEY env, or kismet.password_file"
)

LOC_SUBKEYS = (
    "kismet.common.location.last",
    "kismet.common.location.avg_loc",
    "kismet.common.location.max_loc",
    "kismet.common.location.min_loc",
)


@dataclass
class Aircraft:
    icao: str
    callsign: str
    lat: float
    lon: float
    alt_m: float
    heading: Optional[float] = None
    speed_m_s: Optional[float] = None
    model: Optional[str] = None
    operator: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "icao": self.icao,
            "callsign": self.callsign,
            "lat": round(self.lat, 5),
            "lon": round(self.lon, 5),
            "alt_m": round(self.alt_m, 1),
            "alt_ft": round(self.alt_m * 3.28084),
            "heading": round(self.heading, 1) if self.heading is not None else None,
            "speed_m_s": round(self.speed_m_s, 1) if self.speed_m_s is not None else None,
            "speed_kts": round(self.speed_m_s * 1.94384, 1) if self.speed_m_s is not None else None,
            "model": self.model,
            "operator": self.operator,
        }


def _resolve_password(cfg: KismetCfg) -> str:
    if cfg.password:
        return cfg.password
    if cfg.password_file:
        path = Path(cfg.password_file)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return os.environ.get("KISMET_PASSWORD", "")


def _resolve_api_key(cfg: KismetCfg) -> str:
    if cfg.api_key:
        return cfg.api_key
    return os.environ.get("KISMET_API_KEY", "")


class KismetAuthError(PermissionError):
    """Raised when Kismet rejects credentials (HTTP 401)."""


def _session_key(cfg: KismetCfg) -> tuple:
    return (cfg.url, cfg.username, _resolve_password(cfg), _resolve_api_key(cfg))


def _http_session(cfg: KismetCfg) -> Session:
    global _HTTP, _HTTP_KEY
    key = _session_key(cfg)
    if _HTTP is None or _HTTP_KEY != key:
        _HTTP = requests.Session()
        _HTTP_KEY = key
    return _HTTP


def _auth_params(cfg: KismetCfg) -> tuple[dict[str, str], Optional[tuple[str, str]], dict[str, str]]:
    """Kismet accepts Basic auth, KISMET cookie/param, and user/password GET vars."""
    params: dict[str, str] = {}
    cookies: dict[str, str] = {}
    auth: Optional[tuple[str, str]] = None

    api_key = _resolve_api_key(cfg)
    password = _resolve_password(cfg)

    if api_key:
        params["KISMET"] = api_key
        cookies["KISMET"] = api_key
        return params, auth, cookies

    if cfg.username:
        params["user"] = cfg.username
        params["password"] = password
        auth = (cfg.username, password)

    return params, auth, cookies


def _warm_login(session: Session, cfg: KismetCfg, timeout: float) -> None:
    """Hit a cheap endpoint so Kismet issues a session cookie."""
    if session.cookies.get("KISMET"):
        return
    base = cfg.url.rstrip("/")
    params, auth, cookies = _auth_params(cfg)
    probe = f"{base}/system/status.json"
    r = session.get(
        probe,
        auth=auth,
        params=params or None,
        cookies=cookies or None,
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    if r.status_code == 401:
        raise KismetAuthError(AUTH_HINT)
    r.raise_for_status()


def _parse_json_response(r: requests.Response) -> dict:
    text = (r.text or "").strip()
    if not text:
        raise ValueError(f"empty response from {r.url}")
    if not text.startswith("{") and "json" not in (r.headers.get("content-type") or "").lower():
        raise ValueError(f"non-JSON response from {r.url}: {text[:120]!r}")
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError(f"unexpected JSON type from {r.url}")
    return data


def _dig(obj: Any, *paths: str) -> Any:
    """Read Kismet dotted field names from flat or nested JSON."""
    if not isinstance(obj, dict):
        return None
    for path in paths:
        if path in obj and obj[path] not in (None, ""):
            return obj[path]
        cur: Any = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None


def _geopoint_from_block(block: Any) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Return lat, lon, alt_m, heading, speed_m_s from a location sub-record."""
    if not isinstance(block, dict):
        return None, None, None, None, None

    gp = block.get("kismet.common.location.geopoint")
    lat = lon = None
    if isinstance(gp, (list, tuple)) and len(gp) >= 2:
        lon, lat = float(gp[0]), float(gp[1])

    if lat is None or lon is None:
        lat = _dig(block, "kismet.common.location.lat")
        lon = _dig(block, "kismet.common.location.lon")
        if lat is not None and lon is not None:
            lat, lon = float(lat), float(lon)

    if lat is None or lon is None or (abs(lat) < 0.01 and abs(lon) < 0.01):
        return None, None, None, None, None

    alt = _dig(block, "kismet.common.location.alt")
    heading = _dig(block, "kismet.common.location.heading")
    speed = _dig(block, "kismet.common.location.speed")
    return (
        lat,
        lon,
        float(alt) if alt is not None else None,
        float(heading) if heading is not None else None,
        float(speed) if speed is not None else None,
    )


def _location_from_device(dev: dict) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    loc = dev.get("kismet.device.base.location")
    if not isinstance(loc, dict):
        return None, None, None, None, None

    for subkey in LOC_SUBKEYS:
        lat, lon, alt, heading, speed = _geopoint_from_block(loc.get(subkey))
        if lat is not None:
            return lat, lon, alt, heading, speed

    # Recent track samples (same source Kismet's map uses for trails).
    cloud = _dig(loc, "kismet.device.base.location_cloud") or loc.get("kismet.device.base.location_cloud")
    if isinstance(cloud, dict):
        samples = _dig(cloud, "kis.gps.rrd.samples_100") or cloud.get("kis.gps.rrd.samples_100")
        if isinstance(samples, list):
            for sample in reversed(samples):
                if not isinstance(sample, dict):
                    continue
                gp = sample.get("kismet.historic.location.geopoint")
                if isinstance(gp, (list, tuple)) and len(gp) >= 2:
                    lon, lat = float(gp[0]), float(gp[1])
                    if abs(lat) > 0.01 or abs(lon) > 0.01:
                        alt = sample.get("kismet.historic.location.alt")
                        return lat, lon, float(alt) if alt is not None else None, None, None

    return None, None, None, None, None


def _parse_device(dev: dict) -> Optional[Aircraft]:
    adsb = dev.get("adsb.device")
    if not isinstance(adsb, dict):
        adsb = dev

    icao = _dig(adsb, "adsb.device.icao", "icao")
    if not icao:
        key = _dig(dev, "kismet.device.base.key", "kismet.device.base.macaddr")
        if key:
            icao = str(key).split("_")[0][-6:]
    if not icao:
        return None

    callsign = (_dig(adsb, "adsb.device.callsign", "callsign") or "").strip()
    if not callsign:
        callsign = str(icao).upper()

    lat, lon, alt_m, heading, speed = _location_from_device(dev)
    if lat is None or lon is None:
        return None

    icao_rec = _dig(adsb, "adsb.device.icao_record", "kismet.adsb.icao_record")
    model = operator = None
    if isinstance(icao_rec, dict):
        model = _dig(icao_rec, "adsb.icao.model", "adsb.icao.type")
        operator = _dig(icao_rec, "adsb.icao.owner")
        if model in ("Unknown", "Unknown Aircraft", None):
            model = _dig(icao_rec, "adsb.icao.atype")
        if operator == "Unknown":
            operator = None

    return Aircraft(
        icao=str(icao).upper(),
        callsign=callsign,
        lat=lat,
        lon=lon,
        alt_m=float(alt_m or 0),
        heading=heading,
        speed_m_s=speed,
        model=str(model) if model and model != "Unknown" else None,
        operator=str(operator) if operator else None,
    )


def _parse_map_payload(payload: dict) -> tuple[List[Aircraft], int]:
    devices = payload.get("kismet.adsb.map.devices")
    if not isinstance(devices, list):
        return [], 0

    out: List[Aircraft] = []
    seen = set()
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        ac = _parse_device(dev)
        if ac is None or ac.icao in seen:
            continue
        seen.add(ac.icao)
        out.append(ac)
    return out, len(devices)


def _fetch_map(url: str, cfg: KismetCfg, timeout: float) -> dict:
    session = _http_session(cfg)
    params, auth, cookies = _auth_params(cfg)
    headers = {"Accept": "application/json"}

    paths = [cfg.map_data_path] if cfg.map_data_path else []
    paths.extend(p for p in MAP_SUFFIXES if p not in paths)

    _warm_login(session, cfg, timeout)

    last_err: Optional[Exception] = None
    for path in paths:
        target = urljoin(url if url.endswith("/") else url + "/", path.lstrip("/"))
        try:
            r = session.get(
                target,
                auth=auth,
                params=params or None,
                cookies=cookies or None,
                headers=headers,
                timeout=timeout,
            )
            if r.status_code == 401:
                raise KismetAuthError(AUTH_HINT)
            r.raise_for_status()
            data = _parse_json_response(r)
            log.debug("kismet map ok %s (%d bytes)", target, len(r.content))
            return data
        except KismetAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log.debug("kismet map fetch failed %s: %s", target, exc)

    if last_err:
        raise last_err
    return {}


def kismet_gui_url(cfg: KismetCfg) -> str:
    base = cfg.url.rstrip("/")
    return f"{base}/#adsb.adsb_map"


def fetch_adsb(config: Config) -> dict:
    """Live ADS-B snapshot for the web dashboard."""
    cfg = config.kismet
    if not cfg.enabled or not cfg.url:
        return {
            "enabled": False,
            "online": False,
            "service_up": None,
            "count": 0,
            "raw_devices": 0,
            "aircraft": [],
            "gui_url": None,
        }

    service_up = consumer_is_up(config) if config.sdr_sharing.status_command else None
    now = datetime.now(timezone.utc).isoformat()
    gui = kismet_gui_url(cfg)

    try:
        payload = _fetch_map(cfg.url, cfg, cfg.timeout_seconds)
        aircraft, raw_count = _parse_map_payload(payload)
        return {
            "enabled": True,
            "online": True,
            "service_up": service_up,
            "count": len(aircraft),
            "raw_devices": raw_count,
            "aircraft": [a.to_dict() for a in aircraft],
            "gui_url": gui,
            "updated_at": now,
            "error": None,
        }
    except KismetAuthError as exc:
        global _AUTH_WARN_TS
        now_ts = time.monotonic()
        if now_ts - _AUTH_WARN_TS > 60:
            log.warning("kismet adsb auth: %s", exc)
            _AUTH_WARN_TS = now_ts
        return {
            "enabled": True,
            "online": False,
            "service_up": service_up,
            "count": 0,
            "raw_devices": 0,
            "aircraft": [],
            "gui_url": gui,
            "updated_at": now,
            "error": str(exc),
            "auth_required": True,
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("kismet adsb fetch: %s", exc)
        return {
            "enabled": True,
            "online": False,
            "service_up": service_up,
            "count": 0,
            "raw_devices": 0,
            "aircraft": [],
            "gui_url": gui,
            "updated_at": now,
            "error": str(exc),
        }
