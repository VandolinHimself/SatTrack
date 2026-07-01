"""FastAPI application for the real-time SatTrack dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional, Set

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..kismet_adsb import fetch_adsb, kismet_gui_url
from ..capture import resolve_dry_run, select_backend
from ..config import Config, load_config
from ..live import build_live_scene, build_schedule_snapshot, build_tracks, pass_detail, satellite_now
from ..predict import predict_all
from ..registry import build_registry, registry_has_stale_tles
from ..status_store import merge_status, read_status
from ..telemetry import Telemetry
from ..tle import data_dir

log = logging.getLogger("sattrack.web")

STATIC_DIR = Path(__file__).parent / "static"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _public_base_url(request: Request) -> str:
    """Absolute site URL for Open Graph tags (respects reverse-proxy headers)."""
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if forwarded_host:
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        return f"{scheme}://{forwarded_host.split(',')[0].strip()}".rstrip("/")
    return str(request.base_url).rstrip("/")


def _render_index(request: Request) -> str:
    return _INDEX_HTML.replace("__SITE_URL__", _public_base_url(request))


def _config_summary(config: Config) -> dict:
    return {
        "observer": {
            "name": config.observer.name,
            "lat": config.observer.latitude,
            "lon": config.observer.longitude,
            "elev_m": config.observer.elevation_m,
        },
        "satellites": [
            {
                "name": s.name,
                "norad_id": s.norad_id,
                "freq_mhz": s.freq_mhz,
                "decoder": s.decoder,
                "mode": s.mode,
                "pipeline": s.pipeline,
                "priority": s.priority,
                "enabled": s.enabled,
            }
            for s in config.satellites
        ],
        "dry_run": resolve_dry_run(config),
        "backend": select_backend(config),
        "data_dir": str(data_dir()),
        "kismet": {
            "enabled": config.kismet.enabled,
            "url": config.kismet.url,
            "gui_url": kismet_gui_url(config.kismet) if config.kismet.enabled else None,
        },
    }


class DashboardHub:
    """Single broadcast loop — one payload build per tick, fan-out to all clients."""

    TICK_SECONDS = 1.0
    TRACKS_TTL = 45.0
    SCHEDULE_TTL = 30.0
    TELEMETRY_TTL = 10.0

    def __init__(self, config: Config) -> None:
        self.config = config
        self.registry: dict = {}
        self._registry_at = 0.0
        self.config_summary = _config_summary(config)
        self.clients: Set[WebSocket] = set()
        self.latest_json: Optional[str] = None
        self._tracks: dict = {}
        self._tracks_version = 0
        self._tracks_at = 0.0
        self._schedule: list = []
        self._next_pass = None
        self._schedule_at = 0.0
        self._telemetry = {"stats": {}, "recent_captures": [], "recent_passes": []}
        self._telemetry_at = 0.0
        self._task: Optional[asyncio.Task] = None
        self._refresh_registry(force=True)

    async def start(self) -> None:
        if self._task is None:
            try:
                self.latest_json = await asyncio.to_thread(self._build_json)
            except Exception:  # noqa: BLE001
                log.exception("initial dashboard payload failed")
            self._task = asyncio.create_task(self._broadcast_loop())

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        if self.latest_json:
            await ws.send_text(self.latest_json)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def _broadcast_loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                text = await asyncio.to_thread(self._build_json)
                self.latest_json = text
                dead: list[WebSocket] = []
                for ws in list(self.clients):
                    try:
                        await ws.send_text(text)
                    except Exception:  # noqa: BLE001
                        dead.append(ws)
                for ws in dead:
                    self.clients.discard(ws)
            except Exception:  # noqa: BLE001
                log.exception("dashboard broadcast tick failed")
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.05, self.TICK_SECONDS - elapsed))

    def _registry_needs_refresh(self) -> bool:
        if self._registry_at <= 0:
            return True
        pred = self.config.prediction
        age_h = (time.monotonic() - self._registry_at) / 3600.0
        if age_h >= pred.tle_refresh_interval_hours:
            return True
        horizon_refresh = min(6.0, pred.horizon_hours / 2)
        if age_h >= horizon_refresh:
            return True
        return registry_has_stale_tles(self.registry, pred.tle_max_age_hours)

    def _refresh_registry(self, *, force: bool = False) -> None:
        if not force and not self._registry_needs_refresh():
            return
        log.info("refreshing TLEs from Celestrak for dashboard ...")
        self.registry = build_registry(self.config, refresh=True)
        self._registry_at = time.monotonic()
        self._tracks_at = 0.0
        self._schedule_at = 0.0

    def _refresh_tracks(self) -> None:
        self._tracks = build_tracks(self.registry)
        self._tracks_version += 1
        self._tracks_at = time.monotonic()

    def _refresh_schedule(self) -> None:
        snap = build_schedule_snapshot(self.config, self.registry)
        self._schedule = snap["schedule"]
        self._next_pass = snap["next_pass"]
        self._schedule_at = time.monotonic()

    def _refresh_telemetry(self) -> None:
        db = data_dir() / "sattrack.db"
        if db.exists():
            with Telemetry(db) as t:
                self._telemetry = {
                    "stats": t.stats(),
                    "recent_captures": [dict(r) for r in t.recent_captures(12)],
                    "recent_passes": t.recent_passes(15),
                }
        else:
            self._telemetry = {
                "stats": {},
                "recent_captures": [],
                "recent_passes": [],
            }
        self._telemetry_at = time.monotonic()

    def _build_payload(self) -> dict:
        now = time.monotonic()
        self._refresh_registry()
        if now - self._tracks_at >= self.TRACKS_TTL:
            self._refresh_tracks()
        if now - self._schedule_at >= self.SCHEDULE_TTL:
            self._refresh_schedule()
        if now - self._telemetry_at >= self.TELEMETRY_TTL:
            self._refresh_telemetry()

        scene = build_live_scene(
            self.config,
            self.registry,
            tracks=self._tracks,
            schedule=self._schedule,
            next_pass=self._next_pass,
            include_tracks=False,
            include_schedule=False,
        )
        watcher = read_status()
        payload = merge_status(scene, watcher)
        payload["tracks"] = self._tracks
        payload["tracks_version"] = self._tracks_version
        payload["schedule"] = self._schedule
        payload["next_pass"] = self._next_pass
        payload["config"] = self.config_summary
        payload["telemetry"] = self._telemetry
        payload["adsb"] = fetch_adsb(self.config)
        return payload

    def _build_json(self) -> str:
        return json.dumps(self._build_payload(), default=str)


def _dashboard_payload(config: Config) -> dict:
    """One-shot snapshot for REST endpoints."""
    hub = DashboardHub(config)
    hub._refresh_tracks()
    hub._refresh_schedule()
    hub._refresh_telemetry()
    return hub._build_payload()


def create_app(config_path: Optional[str] = None) -> FastAPI:
    config = load_config(config_path)
    app = FastAPI(title="SatTrack Dashboard", version="1.0")
    hub = DashboardHub(config)

    @app.on_event("startup")
    async def _startup() -> None:
        await hub.start()

    @app.get("/api/health")
    async def health():
        return {"ok": True, "clients": len(hub.clients)}

    @app.get("/api/config")
    async def api_config():
        return hub.config_summary

    @app.get("/api/live")
    async def api_live():
        return await asyncio.to_thread(_dashboard_payload, config)

    @app.get("/api/satellite/{name}")
    async def api_satellite(name: str):
        registry = build_registry(config, refresh=False)
        entry = registry.get(name)
        if entry is None:
            raise HTTPException(404, f"Unknown satellite: {name}")
        return satellite_now(entry, config)

    @app.get("/api/pass/{norad_id}/next")
    async def api_next_pass(norad_id: int):
        registry = build_registry(config, refresh=False)
        entry = next((e for e in registry.values() if e.satellite.norad_id == norad_id), None)
        if entry is None:
            raise HTTPException(404, f"No satellite with NORAD {norad_id}")
        passes = predict_all(config, {entry.name: entry}, hours=24)
        if not passes:
            raise HTTPException(404, "No upcoming pass")
        return pass_detail(config, entry, passes[0])

    @app.get("/api/telemetry/stats")
    async def api_stats():
        db = data_dir() / "sattrack.db"
        if not db.exists():
            return {}
        return await asyncio.to_thread(_telemetry_stats, db)

    @app.get("/api/telemetry/captures")
    async def api_captures(limit: int = 20):
        db = data_dir() / "sattrack.db"
        if not db.exists():
            return []
        return await asyncio.to_thread(lambda: _recent_captures(db, limit))

    @app.get("/api/telemetry/doppler/{pass_id}")
    async def api_doppler(pass_id: int):
        db = data_dir() / "sattrack.db"
        if not db.exists():
            return []
        rows = await asyncio.to_thread(lambda: _doppler_rows(db, pass_id))
        if not rows:
            raise HTTPException(404, "No Doppler samples for pass")
        return rows

    @app.get("/api/adsb")
    async def api_adsb():
        return await asyncio.to_thread(fetch_adsb, config)

    @app.websocket("/ws")
    async def websocket_live(ws: WebSocket):
        await hub.connect(ws)
        try:
            while True:
                await ws.receive()
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("websocket closed: %s", exc)
        finally:
            hub.disconnect(ws)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/your-logo.png")
    async def logo():
        path = REPO_ROOT / "your-logo.png"
        if not path.is_file():
            raise HTTPException(404, "Logo not found")
        return FileResponse(path)

    @app.get("/")
    async def index(request: Request):
        return HTMLResponse(_render_index(request))

    return app


def _telemetry_stats(db: Path) -> dict:
    with Telemetry(db) as t:
        return t.stats()


def _recent_captures(db: Path, limit: int) -> list:
    with Telemetry(db) as t:
        return [dict(r) for r in t.recent_captures(min(limit, 100))]


def _doppler_rows(db: Path, pass_id: int) -> list:
    with Telemetry(db) as t:
        return t.doppler_for_pass(pass_id)
