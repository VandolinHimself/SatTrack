"""Shared live status between the watcher daemon and the web dashboard."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .tle import data_dir

log = logging.getLogger("sattrack.status")

STATUS_FILE = "status.json"


def status_path() -> Path:
    return data_dir() / STATUS_FILE


def write_status(payload: dict) -> None:
    """Atomically publish watcher state for the web UI."""
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


_STALE_PHASES = frozenset({
    "recording", "decoding", "waiting", "handoff", "starting", "refreshing",
})
# Watcher heartbeat defaults to 5s; treat status as stale well before the next tick.
_STALE_SECONDS = 45.0


def _parse_utc(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_watcher_status(watcher: Optional[dict]) -> Optional[dict]:
    """Drop stale daemon state so a stopped watcher cannot pin the UI."""
    if not watcher:
        return watcher
    w = dict(watcher)
    now = datetime.now(timezone.utc)

    updated_raw = w.get("updated_at")
    if updated_raw:
        try:
            age = (now - _parse_utc(updated_raw)).total_seconds()
            if age > _STALE_SECONDS:
                # No heartbeat — treat as if the daemon is not running.
                return None
        except (ValueError, TypeError):
            pass

    ap = w.get("active_pass")
    if ap and ap.get("los"):
        try:
            if _parse_utc(ap["los"]) <= now:
                w["active_pass"] = None
                if w.get("phase") in _STALE_PHASES:
                    w["phase"] = "idle"
        except (ValueError, TypeError):
            pass

    return w


def read_status() -> Optional[dict]:
    path = status_path()
    if not path.exists():
        return None
    try:
        return normalize_watcher_status(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        log.debug("status read failed: %s", exc)
        return None


def merge_status(base: dict, watcher: Optional[dict]) -> dict:
    """Overlay daemon phase info onto a live scene snapshot."""
    out = dict(base)
    if watcher:
        out["watcher"] = watcher
    else:
        out["watcher"] = {
            "phase": "offline",
            "message": "Watcher not running — showing predicted schedule only",
            "active_pass": None,
        }
    return out
