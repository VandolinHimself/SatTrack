"""The daemon: predict -> wait for AOS -> capture -> decode -> log -> repeat.

This is the "Orbitron killer" event loop. It keeps the TLE registry fresh,
predicts the next horizon of passes, sleeps until just before each AOS, tunes
the SDR only for the pass, decodes the result, and writes everything to the
telemetry DB. A single RTL-SDR is assumed, so passes are handled sequentially
(earliest AOS wins; overlapping lower-priority passes are skipped).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from .capture import (
    CaptureResult,
    decode,
    ensure_consumer_up,
    exclusive_sdr,
    record_pass,
    restore_shared_consumer,
    resolve_dry_run,
    select_backend,
)
from .config import Config
from .predict import Pass, doppler_curve, predict_all
from .registry import RegistryEntry, build_registry, registry_has_stale_tles
from .telemetry import Telemetry
from .tle import data_dir

log = logging.getLogger("sattrack.watcher")

PassKey = str


def _key(p: Pass) -> PassKey:
    return f"{p.norad_id}:{p.decoder}:{p.aos.isoformat()}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _capture_window(p: Pass, config: Config) -> tuple[datetime, datetime]:
    """When the SDR must be held for pass ``p`` (pre-AOS through post-LOS)."""
    cap = config.capture
    start = p.aos - timedelta(seconds=cap.pre_aos_seconds)
    end = p.los + timedelta(seconds=cap.post_los_seconds)
    return start, end


def _windows_overlap(a: Pass, b: Pass, config: Config) -> bool:
    a0, a1 = _capture_window(a, config)
    b0, b1 = _capture_window(b, config)
    return a0 < b1 and b0 < a1


def _pass_mostly_missed(p: Pass, now: datetime, frac: float = 0.75) -> bool:
    """True when we'd join a pass so late that most of it is already gone."""
    if now <= p.aos:
        return False
    return (now - p.aos).total_seconds() > frac * p.duration_s


def _pass_too_short(p: Pass, now: datetime, min_remaining: float) -> bool:
    """True when not enough time left to make a capture worthwhile."""
    return (p.los - now).total_seconds() < min_remaining


def plan_schedule(passes: List[Pass], config: Config) -> tuple[List[Pass], Set[PassKey]]:
    """Pick a non-overlapping capture schedule (one RTL-SDR).

    Passes are considered in priority order (highest first), then AOS. When
    capture windows overlap, the pass already selected keeps the slot — so a
    higher-priority pass is never displaced by a later, lower-priority one.
    """
    skipped: Set[PassKey] = set()
    pending = sorted(passes, key=lambda p: (-p.priority, p.aos))
    selected: List[Pass] = []

    for p in pending:
        conflict: Optional[Pass] = None
        for s in selected:
            if _windows_overlap(p, s, config):
                conflict = s
                break
        if conflict is None:
            selected.append(p)
            continue
        skipped.add(_key(p))
        log.info(
            "schedule: skip %s (overlaps %s — %s keeps the slot)",
            p.satellite, conflict.satellite, conflict.satellite,
        )

    selected.sort(key=lambda p: p.aos)
    return selected, skipped


def next_display_pass(
    passes: List[Pass],
    config: Config,
    now: datetime,
    *,
    handled: Optional[Set[PassKey]] = None,
) -> Optional[Pass]:
    """Earliest physical pass for the UI — ignores capture overlap skips."""
    handled = handled or set()
    min_rem = config.watcher.min_remaining_seconds

    for p in sorted(passes, key=lambda p: p.aos):
        pk = _key(p)
        if pk in handled:
            continue
        if p.los <= now:
            continue
        if _pass_too_short(p, now, min_rem):
            continue
        if _pass_mostly_missed(p, now):
            continue
        return p
    return None


class Watcher:
    def __init__(self, config: Config, db_path: Optional[Path] = None):
        self.config = config
        self.db_path = Path(db_path) if db_path else (data_dir() / "sattrack.db")
        self.registry: Dict[str, RegistryEntry] = {}
        self.passes: List[Pass] = []
        self._planned: List[Pass] = []          # non-overlapping capture schedule
        self.handled: Set[PassKey] = set()
        self._skipped: Set[PassKey] = set()   # overlap / missed — never retry
        self.last_refresh: Optional[datetime] = None
        self._stop = False

        # Live status shared with the heartbeat thread.
        self._phase = "starting"
        self._active_pass: Optional[Pass] = None
        self._record_started: Optional[datetime] = None
        self._record_stop_at: Optional[datetime] = None
        self._hb_thread: Optional[threading.Thread] = None
        self._last_watchdog: Optional[float] = None

    # -- lifecycle ---------------------------------------------------------

    def request_stop(self, *_) -> None:
        log.info("stop requested — finishing current step then exiting")
        self._stop = True

    def refresh(self, telemetry: Telemetry) -> None:
        self._phase = "refreshing"
        log.info("refreshing TLEs + pass predictions ...")
        self.registry = build_registry(self.config, refresh=True)
        self.passes = predict_all(self.config, self.registry)
        self._planned, planned_skip = plan_schedule(self.passes, self.config)
        self._skipped |= planned_skip
        telemetry.record_passes(self.passes)
        self.last_refresh = _now()
        self._log_schedule()

    def _needs_refresh(self) -> bool:
        if self.last_refresh is None:
            return True
        pred = self.config.prediction
        age_h = (_now() - self.last_refresh).total_seconds() / 3600.0
        # Periodic Celestrak pull (default every 4h).
        if age_h >= pred.tle_refresh_interval_hours:
            log.debug("TLE refresh due (%.1fh since last pull)", age_h)
            return True
        # Re-predict well before the horizon empties.
        horizon_refresh = min(6.0, pred.horizon_hours / 2)
        if age_h >= horizon_refresh:
            return True
        # Any epoch older than the configured limit — try Celestrak again.
        if self.registry and registry_has_stale_tles(self.registry, pred.tle_max_age_hours):
            log.info("stale TLE epoch detected — refreshing from Celestrak")
            return True
        return self._select_next() is None

    def _select_next(self) -> Optional[Pass]:
        now = _now()
        min_rem = self.config.watcher.min_remaining_seconds
        for p in self._planned:  # already conflict-resolved, sorted by AOS
            pk = _key(p)
            if pk in self.handled or pk in self._skipped:
                continue
            if p.los <= now:
                continue
            if _pass_too_short(p, now, min_rem):
                continue
            if _pass_mostly_missed(p, now):
                continue
            return p
        return None

    def _log_schedule(self, n: int = 6) -> None:
        upcoming = [p for p in self._planned if p.los > _now() and _key(p) not in self._skipped][:n]
        if not upcoming:
            log.info("no upcoming passes in schedule")
            return
        log.info("capture schedule (%d passes, overlaps resolved):", len(upcoming))
        for p in upcoming:
            w0, w1 = _capture_window(p, self.config)
            log.info(
                "  %s  %s  el=%4.1f deg  dur=%3.0fs  %.4f MHz  window %s–%s",
                p.aos.astimezone().strftime("%a %H:%M:%S"),
                p.satellite, p.max_elevation_deg, p.duration_s, p.freq_mhz,
                w0.astimezone().strftime("%H:%M"), w1.astimezone().strftime("%H:%M"),
            )

    # -- heartbeat ---------------------------------------------------------

    def _next_pass_for_display(self) -> Optional[Pass]:
        """Pass shown in the dashboard 'Next pass' card."""
        now = _now()
        if self._phase in ("recording", "decoding", "waiting") and self._active_pass is not None:
            return self._active_pass
        return next_display_pass(
            self.passes,
            self.config,
            now,
            handled=self.handled,
        )

    def _status_payload(self) -> dict:
        """JSON snapshot for the web dashboard."""
        now = _now()
        nxt = self._next_pass_for_display()
        queued = sum(
            1 for p in self._planned
            if p.los > now and _key(p) not in self.handled and _key(p) not in self._skipped
        )
        payload: dict = {
            "phase": self._phase,
            "message": self._status_line(),
            "active_pass": self._active_pass.to_dict() if self._active_pass else None,
            "next_pass": nxt.to_dict() if nxt else None,
            "queued_passes": queued,
            "dry_run": resolve_dry_run(self.config),
            "backend": select_backend(self.config),
        }
        if self._phase == "recording" and self._active_pass and self._record_started:
            elapsed = (now - self._record_started).total_seconds()
            ends_in = (
                (self._record_stop_at - now).total_seconds()
                if self._record_stop_at else 0
            )
            payload["recording"] = {
                "elapsed_s": round(elapsed),
                "ends_in_s": round(max(0, ends_in)),
            }
        return payload

    def _status_line(self) -> str:
        now = _now()
        if self._phase == "recording" and self._active_pass is not None:
            p = self._active_pass
            ends_in = (self._record_stop_at - now).total_seconds() if self._record_stop_at else 0
            elapsed = (now - self._record_started).total_seconds() if self._record_started else 0
            return (
                f"[recording] {p.satellite} | el(max) {p.max_elevation_deg:.0f}° | "
                f"{p.freq_mhz:.4f} MHz | elapsed {_mmss(elapsed)} | ends in {_mmss(ends_in)}"
            )

        if self._phase == "decoding" and self._active_pass is not None:
            return f"[decoding] {self._active_pass.satellite} | running decoder pipeline ..."

        if self._phase == "handoff":
            nxt = self._select_next()
            if nxt is not None:
                return f"[handoff] SDR/kismet settling — next: {nxt.satellite} in {_mmss((nxt.aos - now).total_seconds())}"
            return "[handoff] SDR/kismet settling before next pass ..."

        nxt = self._next_pass_for_display()
        if nxt is not None:
            queued = sum(
                1 for p in self._planned
                if p.los > now and _key(p) not in self.handled and _key(p) not in self._skipped
            )
            if nxt.aos <= now:
                aos_part = "overhead now"
            else:
                aos_part = f"AOS in {_mmss((nxt.aos - now).total_seconds())}"
            return (
                f"[waiting] next: {nxt.satellite} | {aos_part} "
                f"| max el {nxt.max_elevation_deg:.0f}° | {nxt.freq_mhz:.4f} MHz "
                f"| {queued} pass(es) queued"
            )

        if self._phase == "refreshing":
            return "[refreshing] updating TLEs + pass predictions ..."
        return "[idle] no passes in horizon — waiting for next prediction refresh"

    def _heartbeat_loop(self) -> None:
        from .status_store import write_status

        interval = max(1.0, float(self.config.watcher.heartbeat_seconds))
        while not self._stop:
            try:
                log.info("%s", self._status_line())
                write_status(self._status_payload())
                self._watchdog()
            except Exception:  # noqa: BLE001 - heartbeat must never crash the daemon
                pass
            end = time.monotonic() + interval
            while not self._stop and time.monotonic() < end:
                time.sleep(0.25)

    # -- main loop ---------------------------------------------------------

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self.request_stop)

        backend = select_backend(self.config)
        mode = {
            "dry": "DRY-RUN (no SDR hardware)",
            "satdump_live": "LIVE (SatDump + RTL-SDR)",
            "rtl_fm": "LIVE (rtl_fm | sox + aptdec)",
        }.get(backend, backend)
        log.info("SatTrack watcher starting — %s", mode)
        log.info("observer: %s (%.4f, %.4f)", self.config.observer.name,
                 self.config.observer.latitude, self.config.observer.longitude)
        log.info("heartbeat every %.0fs", self.config.watcher.heartbeat_seconds)

        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="sattrack-heartbeat", daemon=True
        )
        self._hb_thread.start()

        try:
            from .status_store import write_status
            write_status(self._status_payload())
        except Exception:  # noqa: BLE001
            pass

        # Make sure kismet (or whatever shares the SDR) is up before we start.
        self._watchdog(force=True)

        with Telemetry(self.db_path) as telemetry:
            while not self._stop:
                self._watchdog()
                if self._needs_refresh():
                    self.refresh(telemetry)

                nxt = self._select_next()
                if nxt is None:
                    self._phase = "idle"
                    self._active_pass = None
                    self._sleep(min(60, self.config.watcher.heartbeat_seconds))
                    continue

                if not self._wait_for_aos(nxt):
                    self.handled.add(_key(nxt))
                    continue  # interrupted or pass missed during wait

                self._handle_pass(nxt, telemetry)
                self.handled.add(_key(nxt))
                self._handoff_after_pass(nxt)

        log.info("watcher stopped")
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=2)

    def _wait_for_aos(self, p: Pass) -> bool:
        """Sleep until pre_aos seconds before AOS. Returns False if missed/stopped."""
        self._phase = "waiting"
        self._active_pass = p
        start = p.aos - timedelta(seconds=self.config.capture.pre_aos_seconds)
        while not self._stop:
            now = _now()
            if now >= start:
                if _pass_mostly_missed(p, now) or _pass_too_short(p, now, self.config.watcher.min_remaining_seconds):
                    return False
                return True
            if p.los <= now:
                return False
            remaining = (start - now).total_seconds()
            self._watchdog()  # keep kismet alive right up until we need the SDR
            # Heartbeat thread prints the countdown; just sleep responsively.
            self._sleep(min(remaining, self.config.watcher.heartbeat_seconds))
        return False

    def _handle_pass(self, p: Pass, telemetry: Telemetry) -> None:
        pass_id = telemetry.record_pass(p)
        stop_at = p.los + timedelta(seconds=self.config.capture.post_los_seconds)
        w0, w1 = _capture_window(p, self.config)

        self._phase = "recording"
        self._active_pass = p
        self._record_started = _now()
        self._record_stop_at = stop_at

        log.info(
            "=== PASS START: %s  el=%.0f deg  capture until %s  (window %s–%s) ===",
            p.satellite, p.max_elevation_deg,
            stop_at.astimezone().strftime("%H:%M:%S"),
            w0.astimezone().strftime("%H:%M"), w1.astimezone().strftime("%H:%M"),
        )

        live = select_backend(self.config) != "dry"
        with exclusive_sdr(self.config, active=live):
            result = record_pass(self.config, p, stop_at)

        # Decode offline (no SDR) — kismet restored in exclusive_sdr; verify after decode.
        self._phase = "decoding"
        self._record_stop_at = None
        log.info("handoff: decoding %s (SDR free, kismet may resume) ...", p.satellite)
        result = decode(self.config, result)
        restore_shared_consumer(self.config)
        self._last_watchdog = time.monotonic()
        telemetry.record_capture(result, pass_id=pass_id)

        entry = self.registry.get(p.satellite)
        if entry is not None:
            try:
                curve = doppler_curve(self.config, entry, p)
                telemetry.record_doppler(pass_id, p.satellite, curve)
            except Exception as exc:  # noqa: BLE001
                log.debug("doppler logging failed: %s", exc)

        self._log_result(result)
        self._mark_overlap_skips(p)

    def _mark_overlap_skips(self, captured: Pass) -> None:
        """Any pass whose window overlapped this capture is dead — don't retry."""
        _, cap_end = _capture_window(captured, self.config)
        for p in self.passes:
            pk = _key(p)
            if pk in self.handled or pk in self._skipped or pk == _key(captured):
                continue
            p_start, _ = _capture_window(p, self.config)
            if p_start < cap_end and p.los > captured.aos:
                self._skipped.add(pk)
                log.info(
                    "handoff: skip %s — overlapped %s capture window",
                    p.satellite, captured.satellite,
                )

    def _handoff_after_pass(self, completed: Pass) -> None:
        """Clean transition before the next listen: settle, verify kismet, announce next."""
        cap = self.config.capture
        share = self.config.sdr_sharing
        self._phase = "handoff"

        cd = cap.post_pass_cooldown_seconds
        if cd > 0:
            log.info("handoff: SDR cooling down %ds ...", cd)
            self._sleep(cd)

        extra = cap.post_handoff_settle_seconds
        if share.enabled and extra > 0:
            log.info("handoff: waiting %ds for kismet/SDR to stabilise ...", extra)
            self._sleep(extra)

        ensure_consumer_up(self.config, force=True)
        self._last_watchdog = time.monotonic()

        self._active_pass = None
        self._record_started = None

        nxt = self._select_next()
        if nxt is not None:
            log.info(
                "handoff: complete — next listen: %s in %s (el %.0f°, %.4f MHz)",
                nxt.satellite,
                _mmss(max(0, (nxt.aos - _now()).total_seconds())),
                nxt.max_elevation_deg,
                nxt.freq_mhz,
            )
        else:
            log.info("handoff: complete — nothing scheduled until next refresh")

        self._phase = "idle"

    def _log_result(self, r: CaptureResult) -> None:
        if r.ok:
            log.info(
                "=== PASS DONE: %s  %s  snr=%s  images=%d ===",
                r.sat_pass.satellite,
                "(dry-run)" if r.dry_run else f"{r.recorded_seconds:.0f}s",
                f"{r.snr_db:.1f}dB" if r.snr_db is not None else "n/a",
                len(r.image_paths),
            )
        else:
            log.error("=== PASS FAILED: %s — %s ===", r.sat_pass.satellite, r.error)

    def _watchdog(self, force: bool = False) -> None:
        """Keep kismet running whenever the RTL-SDR is not actively recording."""
        if self._phase == "recording":
            return
        share = self.config.sdr_sharing
        if not (share.enabled and share.watchdog):
            return
        interval = share.watchdog_interval_seconds
        now = time.monotonic()
        if not force and self._last_watchdog is not None and now - self._last_watchdog < interval:
            return
        self._last_watchdog = now
        try:
            ensure_consumer_up(self.config, force=force)
        except Exception as exc:  # noqa: BLE001
            log.debug("watchdog error: %s", exc)

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep in 1s slices so Ctrl-C is responsive."""
        end = time.monotonic() + max(0.0, seconds)
        while not self._stop and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))
