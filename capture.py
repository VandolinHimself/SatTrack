"""Event-driven RTL-SDR capture + decode pipeline.

The watcher calls :func:`record_pass` at AOS and the SDR is tuned *only* for
the duration of the pass, then :func:`decode` finalises imagery.

Two capture backends:

* ``satdump_live`` (default when SatDump is installed) — SatDump tunes the
  RTL-SDR itself, FM-demods, syncs APT and writes georeferenced images in one
  process. This is the canonical SatDump prod path. (SatDump v2 cannot ingest
  an FM-demodulated *audio* wav for APT, so we don't try to.)
* ``rtl_fm`` — classic ``rtl_fm | sox`` to an audio wav, decoded by ``aptdec``.
  Useful as a fallback / for SNR logging.

On Windows or any host without SDR tooling everything runs in ``dry_run``
mode: the schedule executes and files are stubbed, no hardware is touched.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

from .config import Config, is_linux

if TYPE_CHECKING:  # avoid importing skyfield (via predict) at runtime
    from .predict import Pass

log = logging.getLogger("sattrack.capture")


# --- tool discovery -------------------------------------------------------

def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _gr_satellites_wrapper() -> Optional[Path]:
    """Bundled launcher that fixes PYTHONPATH + GNU Radio import order."""
    p = Path(__file__).resolve().parent.parent / "scripts" / "gr_satellites.sh"
    return p if p.is_file() else None


def gr_satellites_cmd() -> Optional[str]:
    """Shell command prefix for gr_satellites (prefers our wrapper)."""
    w = _gr_satellites_wrapper()
    if w is not None:
        return f'"{w}"'
    if have("gr_satellites"):
        return "gr_satellites"
    return None


def gr_satellites_available() -> bool:
    """True only if gr_satellites actually runs (not just present on PATH)."""
    cmd = gr_satellites_cmd()
    if cmd is None:
        return False
    try:
        r = subprocess.run(
            f"{cmd} --list_satellites",
            shell=True,
            timeout=60,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def hardware_available() -> bool:
    if not is_linux():
        return False
    # SatDump offline/live needs rtl_sdr; FM targets need rtl_fm+sox.
    if have("satdump") and have("rtl_sdr"):
        return True
    return have("rtl_fm") and have("sox")


def select_backend(config: Config) -> str:
    """Resolve the effective capture backend: 'satdump_live', 'rtl_fm', or 'dry'."""
    if resolve_dry_run(config):
        return "dry"
    requested = config.capture.backend
    if requested == "rtl_fm" and have("rtl_fm") and have("sox"):
        return "rtl_fm"
    if requested == "satdump_live" and have("satdump"):
        return "satdump_live"
    # Auto: prefer satdump_live, else rtl_fm.
    if have("satdump"):
        return "satdump_live"
    if have("rtl_fm") and have("sox"):
        return "rtl_fm"
    return "dry"


def resolve_dry_run(config: Config) -> bool:
    if config.capture.dry_run is not None:
        return bool(config.capture.dry_run)
    return not hardware_available()


def _run_share_cmd(cmd: str) -> bool:
    """Run an SDR release/reacquire command; log but never raise."""
    try:
        r = subprocess.run(cmd, shell=True, timeout=60,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if r.returncode != 0:
            log.warning("SDR-share cmd returned %d: %s | %s",
                        r.returncode, cmd, r.stdout.decode(errors="ignore")[:200])
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("SDR-share cmd failed: %s (%s)", cmd, exc)
        return False


def consumer_is_up(config: Config) -> Optional[bool]:
    """Is the shared-SDR consumer (e.g. kismet) running? None if unknown."""
    share = config.sdr_sharing
    if not share.status_command:
        return None
    try:
        r = subprocess.run(share.status_command, shell=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception as exc:  # noqa: BLE001
        log.debug("status command failed: %s (%s)", share.status_command, exc)
        return None


def restore_shared_consumer(config: Config) -> None:
    """Hand the RTL-SDR back to the shared consumer after capture/decoding."""
    share = config.sdr_sharing
    if not (share.enabled and share.reacquire_command):
        return
    log.info("restoring shared-SDR consumer: %s", share.reacquire_command)
    if share.release_command:
        _run_share_cmd(share.release_command)
    _run_share_cmd(share.reacquire_command)
    if share.settle_seconds > 0:
        time.sleep(share.settle_seconds)


def ensure_consumer_up(config: Config, force: bool = False, restart: bool = False) -> None:
    """Watchdog: start the shared-SDR consumer if it isn't running.

    ``force`` (used at startup) also starts it when status is unknown.
    ``restart`` runs a full stop/start restore (e.g. after a capture).
    """
    if restart:
        restore_shared_consumer(config)
        return
    share = config.sdr_sharing
    if not (share.enabled and share.reacquire_command):
        return
    if not (share.watchdog or force):
        return
    up = consumer_is_up(config)
    if up is True:
        return
    if up is None and not force:
        return  # can't tell and not startup — don't blindly poke it
    reason = "down" if up is False else "status unknown"
    log.info("watchdog: shared-SDR consumer %s — starting: %s",
             reason, share.reacquire_command)
    _run_share_cmd(share.reacquire_command)
    if share.settle_seconds > 0:
        time.sleep(share.settle_seconds)


@contextlib.contextmanager
def exclusive_sdr(config: Config, active: bool = True) -> Iterator[None]:
    """Hold exclusive RTL-SDR access for the duration of the block.

    Releases a competing consumer (e.g. kismet) before yielding and restores
    it afterwards — guaranteed via ``finally`` even if the capture raises.
    No-op when disabled or when ``active`` is False (e.g. dry-run).
    """
    share = config.sdr_sharing
    do_it = active and share.enabled and bool(share.release_command)
    released = False
    if do_it:
        log.info("releasing SDR for capture: %s", share.release_command)
        released = _run_share_cmd(share.release_command)
        if not released:
            log.warning("SDR release may have failed — capture could find the device busy")
        if share.settle_seconds > 0:
            time.sleep(share.settle_seconds)
    try:
        yield
    finally:
        if do_it and share.reacquire_command:
            log.info("reacquiring SDR (resuming other consumer): %s", share.reacquire_command)
            _run_share_cmd(share.reacquire_command)
            if share.settle_seconds > 0:
                time.sleep(share.settle_seconds)


_SAT_NUM = {25338: 15, 28654: 18, 33591: 19}


def _satellite_number(sat_pass: "Pass") -> Optional[int]:
    if sat_pass.norad_id in _SAT_NUM:
        return _SAT_NUM[sat_pass.norad_id]
    # Fall back to a trailing number in the name, e.g. "NOAA 19" -> 19.
    digits = "".join(ch for ch in sat_pass.satellite if ch.isdigit())
    return int(digits) if digits else None


# --- result type ----------------------------------------------------------

@dataclass
class CaptureResult:
    sat_pass: Pass
    wav_path: Optional[Path] = None
    image_paths: list[Path] = field(default_factory=list)
    snr_db: Optional[float] = None
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    dry_run: bool = False
    backend: str = "dry"
    decoded: bool = False  # True when capture already produced images (live)
    samplerate: Optional[int] = None  # IQ sample rate (rtl_sdr_iq backend)
    raw_path: Optional[Path] = None   # raw IQ baseband (satdump offline path)
    ok: bool = False
    error: Optional[str] = None

    @property
    def recorded_seconds(self) -> Optional[float]:
        if self.started_at and self.stopped_at:
            return (self.stopped_at - self.started_at).total_seconds()
        return None


# --- recording ------------------------------------------------------------

def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text).strip("_")


def _basename(sat_pass: Pass) -> str:
    ts = sat_pass.aos.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}_{_slug(sat_pass.satellite)}_{round(sat_pass.max_elevation_deg)}deg"


def _uses_satdump(sat_pass: Pass) -> bool:
    return sat_pass.decoder in ("satdump", "noaa_apt")


_satdump_ver: Optional[tuple[int, int, int]] = None


def satdump_version() -> Optional[tuple[int, int, int]]:
    """Parse SatDump version from CLI banner, e.g. (1, 2, 3) or (2, 0, 0)."""
    global _satdump_ver
    if _satdump_ver is not None:
        return _satdump_ver
    if not have("satdump"):
        return None

    def _parse(text: str) -> Optional[tuple[int, int, int]]:
        for pat in (
            r"v(\d+)\.(\d+)\.(\d+)",
            r"SatDump[^\d]{0,40}(\d+)\.(\d+)\.(\d+)",
            r"version[^\d]{0,20}(\d+)\.(\d+)\.(\d+)",
            r"(?<![\d.])(\d+)\.(\d+)\.(\d+)(?![\d.])",
        ):
            m = re.search(pat, text, re.I)
            if m:
                return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return None

    probes = (
        ["satdump"],
        ["satdump", "--help"],
        ["satdump", "help"],
        ["satdump", "--version"],
    )
    for argv in probes:
        try:
            r = subprocess.run(
                argv, capture_output=True, text=True, timeout=15,
            )
            ver = _parse((r.stdout or "") + (r.stderr or ""))
            if ver:
                _satdump_ver = ver
                return _satdump_ver
        except Exception:  # noqa: BLE001
            continue

    # Binary exists but banner has no semver — assume apt v1.x (offline IQ path).
    log.debug("satdump found but version string not parsed; assuming v1.x")
    _satdump_ver = (1, 2, 0)
    return _satdump_ver


def use_satdump_offline(config: Config) -> bool:
    """True when we should record IQ with rtl_sdr and decode offline (not live)."""
    mode = (config.capture.satdump_mode or "auto").lower()
    if mode == "offline":
        return True
    if mode == "live":
        return False
    ver = satdump_version()
    # apt SatDump v1.x live mode crashes on plugin load on many Kali builds.
    return ver is None or ver[0] < 2


def record_pass(
    config: Config,
    sat_pass: Pass,
    stop_at: datetime,
    dry_run: Optional[bool] = None,
) -> CaptureResult:
    """Capture a pass until ``stop_at`` (UTC). Blocks for the pass duration.

    The method is chosen per-target by its decoder: SatDump-pipeline targets
    use ``satdump live`` (capture+decode in one), everything else (APRS, SSTV,
    FM voice) records FM audio via ``rtl_fm`` and is decoded afterwards.
    """
    dry = (dry_run is True) or (dry_run is None and resolve_dry_run(config))
    out_dir = config.resolve_output_dir()

    if dry:
        result = CaptureResult(sat_pass=sat_pass, dry_run=True, backend="dry")
        result.started_at = datetime.now(timezone.utc)
        base = out_dir / _basename(sat_pass)
        if _uses_satdump(sat_pass) and have("satdump"):
            result.backend = "satdump_offline" if use_satdump_offline(config) else "satdump_live"
            if use_satdump_offline(config):
                result.raw_path = base.with_suffix(".cu8")
            else:
                result.raw_path = None
        result.wav_path = base.with_suffix(".wav")
        return _simulate_record(result, stop_at)

    if _uses_satdump(sat_pass) and have("satdump"):
        result = CaptureResult(sat_pass=sat_pass, backend="satdump_offline" if use_satdump_offline(config) else "satdump_live")
        result.started_at = datetime.now(timezone.utc)
        if use_satdump_offline(config):
            return _record_satdump_offline(config, sat_pass, stop_at, result)
        return _record_satdump_live(config, sat_pass, stop_at, result)

    if sat_pass.mode == "iq":
        result = CaptureResult(sat_pass=sat_pass, backend="rtl_sdr_iq")
        result.started_at = datetime.now(timezone.utc)
        return _record_iq(config, sat_pass, stop_at, result)

    result = CaptureResult(sat_pass=sat_pass, backend="rtl_fm")
    result.started_at = datetime.now(timezone.utc)
    return _record_rtl_fm(config, sat_pass, stop_at, result)


def _seconds_until(stop_at: datetime) -> int:
    return max(1, int((stop_at - datetime.now(timezone.utc)).total_seconds()))


def _satdump_pipeline(sat_pass: Pass) -> str:
    if sat_pass.pipeline:
        return sat_pass.pipeline
    return "noaa_apt" if sat_pass.decoder == "noaa_apt" else sat_pass.decoder


def _satdump_satellite_number(sat_pass: Pass) -> Optional[str]:
    """SatDump ``--satellite_number`` for pipelines that need it (Meteor, NOAA APT)."""
    if getattr(sat_pass, "satdump_satellite_number", None):
        return sat_pass.satdump_satellite_number
    name = sat_pass.satellite.upper()
    if "M2 3" in name or "M2-3" in name:
        return "M2-3"
    if "M2 2" in name or "M2-2" in name:
        return "M2-2"
    if "M2 4" in name or "M2-4" in name:
        return "M2-4"
    if "METEOR" in name and "M2" in name:
        return "M2"
    return None


def _satdump_pipeline_variants(pipeline: str) -> list[str]:
    """Try alternate pipeline spellings (v1 apt builds often lack ``*-x_*`` ids)."""
    variants = [pipeline]
    if pipeline == "meteor_m2-x_lrpt":
        variants.append("meteor_m2_lrpt")
    elif pipeline == "meteor_m2_lrpt":
        variants.append("meteor_m2-x_lrpt")
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for p in variants:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _satdump_wrapper() -> Optional[Path]:
    p = Path(__file__).resolve().parent.parent / "scripts" / "satdump.sh"
    return p if p.is_file() else None


def satdump_cmd() -> str:
    """Shell prefix for satdump (bundled wrapper if present)."""
    w = _satdump_wrapper()
    if w is not None:
        return f'bash "{w}"'
    return "satdump"


def satdump_plugin_crash(output: str, rc: int) -> bool:
    """True when apt SatDump died in the broken-plugin loader (SIGABRT / exit 134)."""
    if rc in (134, -6, 139, -11):
        return True
    text = output.lower()
    return any(x in text for x in (
        "std::out_of_range", "firstparty_loader", "terminate called", "aborted",
    ))


def satdump_smoke_test() -> tuple[bool, str]:
    """Return (ok, message). Requires fix_satdump_plugins.sh on broken apt builds."""
    if not have("satdump"):
        return False, "satdump not installed"
    rc, out = _run(f"{satdump_cmd()} 2>&1", timeout=30)
    snippet = "\n".join(out.splitlines()[:20])
    if satdump_plugin_crash(snippet, rc):
        return False, f"plugin crash (exit {rc}) — run: sudo bash scripts/fix_satdump_plugins.sh"
    low = out.lower()
    # Bare `satdump` prints banner then exits 1 asking for live/record/pipeline — that is OK.
    if "starting satdump" in low and (
        rc == 0
        or "please specify" in low
        or "live/record or pipeline" in low
    ):
        return True, "OK"
    return False, f"unexpected exit {rc}: {snippet[:200]}"


def _satdump_offline_commands(
    pipeline: str,
    raw: Path,
    out_dir: Path,
    samplerate: int,
    sat_pass: Pass,
) -> list[str]:
    """Build SatDump offline-decode command lines to try (v1 legacy + v2 pipeline)."""
    ver = satdump_version()
    extras: list[str] = [f"--samplerate {samplerate}", "--baseband_format cu8"]
    sat_num = _satdump_satellite_number(sat_pass)
    if sat_num:
        extras.append(f'--satellite_number {sat_num}')
    if pipeline == "noaa_apt":
        apt_num = _satellite_number(sat_pass)
        if apt_num is not None:
            extras.append(f"--satellite_number {apt_num}")
    opt = " ".join(extras)
    raw_q, out_q = f'"{raw.resolve()}"', f'"{out_dir.resolve()}"'

    sd = satdump_cmd()
    cmds: list[str] = []
    for pl in _satdump_pipeline_variants(pipeline):
        if ver and ver[0] >= 2:
            cmds.append(f"{sd} pipeline {pl} baseband {raw_q} {out_q} {opt}")
        else:
            # v1.x: ``satdump <pipeline> baseband ...`` (no ``pipeline`` subcommand)
            cmds.append(f"{sd} {pl} baseband {raw_q} {out_q} {opt}")
            cmds.append(f"{sd} pipeline {pl} baseband {raw_q} {out_q} {opt}")
    # de-dupe
    seen: set[str] = set()
    unique: list[str] = []
    for c in cmds:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def preview_satdump_offline_command(config: Config, sat_pass: Pass) -> str:
    """One-line example of the offline decode command (for doctor / debugging)."""
    pipeline = _satdump_pipeline(sat_pass)
    rate = sat_pass.samplerate or config.capture.live_sample_rate
    raw = config.resolve_output_dir() / "example.cu8"
    out = config.resolve_output_dir() / "example_out"
    cmds = _satdump_offline_commands(pipeline, raw, out, rate, sat_pass)
    return cmds[0] if cmds else f"satdump {pipeline} baseband ..."


def preview_satdump_live_command(config: Config, sat_pass: Pass, timeout_s: int = 600) -> str:
    cap = config.capture
    out = config.resolve_output_dir() / "example_out"
    return _build_satdump_live(config, sat_pass, out, timeout_s)


def _build_satdump_live(config: Config, sat_pass: Pass, out_dir: Path, timeout_s: int) -> str:
    """`satdump live` — SDR capture + pipeline decode in one process."""
    cap = config.capture
    freq_hz = int(round(sat_pass.freq_mhz * 1e6))
    samplerate = sat_pass.samplerate or cap.live_sample_rate
    start_ts = int(datetime.now(timezone.utc).timestamp())
    pipeline = _satdump_pipeline(sat_pass)
    parts = [
        "satdump", "live", pipeline, f'"{out_dir}"',
        "--source", "rtlsdr",
        "--source_id", str(cap.device_index),
        "--samplerate", str(samplerate),
        "--frequency", str(freq_hz),
        "--gain", str(cap.gain),
        "--start_timestamp", str(start_ts),
        "--timeout", str(timeout_s),
        "--finish_processing",
    ]
    if cap.bias_tee:
        parts.append("--bias")
    # APT projections need the satellite number; harmless to omit elsewhere.
    if pipeline == "noaa_apt":
        sat_num = _satellite_number(sat_pass)
        if sat_num is not None:
            parts += ["--satellite_number", str(sat_num)]
    return " ".join(parts)


def _record_satdump_live(
    config: Config, sat_pass: Pass, stop_at: datetime, result: CaptureResult
) -> CaptureResult:
    out_dir = config.resolve_output_dir() / _basename(sat_pass)
    out_dir.mkdir(parents=True, exist_ok=True)
    timeout_s = _seconds_until(stop_at)
    cmd = _build_satdump_live(config, sat_pass, out_dir, timeout_s)

    log.info("satdump live: %s for %ds -> %s", sat_pass.satellite, timeout_s, out_dir)
    log.info("cmd: %s", cmd)
    try:
        r = subprocess.run(
            cmd, shell=True, timeout=timeout_s + 600,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        log_path = out_dir / "satdump_live.log"
        log_path.write_text(r.stdout.decode(errors="ignore"), encoding="utf-8")
        result.stopped_at = datetime.now(timezone.utc)
        result.image_paths = _find_images(out_dir)
        result.decoded = True
        result.ok = bool(result.image_paths) and r.returncode == 0
        if r.returncode != 0:
            result.error = f"satdump crashed/exited {r.returncode} — see {log_path.name}"
            log.error(result.error)
        elif not result.ok:
            result.error = "satdump produced no images (weak pass / no signal?)"
            log.warning(result.error)
    except subprocess.TimeoutExpired:
        result.stopped_at = datetime.now(timezone.utc)
        result.image_paths = _find_images(out_dir)
        result.decoded = True
        result.ok = bool(result.image_paths)
        result.error = None if result.ok else "satdump timed out"
    except Exception as exc:  # noqa: BLE001
        result.stopped_at = datetime.now(timezone.utc)
        result.error = f"satdump live failed: {exc}"
        log.error(result.error)
    return result


def _build_rtl_sdr_raw(config: Config, sat_pass: Pass, raw_path: Path) -> str:
    """rtl_sdr raw cu8 IQ capture to a file (for offline SatDump decode)."""
    cap = config.capture
    rate = sat_pass.samplerate or cap.live_sample_rate
    freq_hz = int(round(sat_pass.freq_mhz * 1e6))
    return (
        f"rtl_sdr -f {freq_hz} -s {rate} -d {cap.device_index} "
        f"-g {cap.gain} -p {cap.ppm} \"{raw_path}\""
    )


def _record_satdump_offline(
    config: Config, sat_pass: Pass, stop_at: datetime, result: CaptureResult
) -> CaptureResult:
    """Record IQ with rtl_sdr; decode with SatDump offline after the pass."""
    out_dir = config.resolve_output_dir() / _basename(sat_pass)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f"{_basename(sat_pass)}.cu8"
    result.raw_path = raw_path
    result.samplerate = sat_pass.samplerate or config.capture.live_sample_rate
    cmd = _build_rtl_sdr_raw(config, sat_pass, raw_path)
    ver = satdump_version()
    log.info(
        "satdump offline (v%s): recording IQ %dsps %s -> %s",
        f"{ver[0]}.{ver[1]}.{ver[2]}" if ver else "?",
        result.samplerate, sat_pass.satellite, raw_path.name,
    )
    _run_until(cmd, stop_at, result, raw_path, "rtl_sdr")
    return result


def _audio_rate_for(sat_pass: Pass, default: int) -> int:
    if sat_pass.decoder == "aprs":
        return 22050           # AFSK1200 — direwolf/atest friendly
    if sat_pass.decoder == "sstv":
        return 11025
    if sat_pass.decoder == "gr_satellites":
        return 48000           # gr-satellites' standard audio rate
    return default


def _build_rtl_fm(config: Config, sat_pass: Pass, wav_path: Path) -> str:
    """rtl_fm -> sox pipeline producing a mono wav for the target's mode."""
    cap = config.capture
    freq = sat_pass.freq_mhz
    demod = "wbfm" if sat_pass.mode == "wfm" else "fm"
    demod_rate = sat_pass.samplerate or cap.sample_rate
    out_rate = _audio_rate_for(sat_pass, cap.audio_rate)
    # de-emphasis only helps broadcast/APT FM; skip for data modes (APRS).
    deemp = "" if sat_pass.decoder == "aprs" else "-E deemp "
    return (
        f"rtl_fm -f {freq}M -M {demod} -s {demod_rate} "
        f"-d {cap.device_index} -g {cap.gain} -p {cap.ppm} {deemp}-F 9 - "
        f"| sox -t raw -e signed -c 1 -b 16 -r {demod_rate} - "
        f'"{wav_path}" rate {out_rate}'
    )


def _run_until(
    cmd: str, stop_at: datetime, result: CaptureResult, wav_path: Path, tool: str
) -> None:
    """Run a recording pipeline until ``stop_at``; populate result.ok/error."""
    log.debug("pipeline: %s", cmd)
    try:
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        while datetime.now(timezone.utc) < stop_at:
            if proc.poll() is not None:
                _, err = proc.communicate()
                raise RuntimeError(
                    f"{tool} exited early ({proc.returncode}): "
                    f"{err.decode(errors='ignore')[:200]}"
                )
            time.sleep(1.0)
        _terminate(proc)
        result.stopped_at = datetime.now(timezone.utc)
        result.ok = wav_path.exists() and wav_path.stat().st_size > 1024
        if not result.ok:
            result.error = "recording produced no/empty output file"
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
        result.stopped_at = datetime.now(timezone.utc)
        log.error("recording failed for %s: %s", result.sat_pass.satellite, exc)


def _record_rtl_fm(
    config: Config, sat_pass: Pass, stop_at: datetime, result: CaptureResult
) -> CaptureResult:
    wav_path = config.resolve_output_dir() / f"{_basename(sat_pass)}.wav"
    result.wav_path = wav_path
    cmd = _build_rtl_fm(config, sat_pass, wav_path)
    log.info("recording %s (FM audio) -> %s", sat_pass.satellite, wav_path.name)
    _run_until(cmd, stop_at, result, wav_path, "rtl_fm")
    if result.ok:
        result.snr_db = estimate_snr(wav_path)
    return result


def _build_rtl_sdr_iq(config: Config, sat_pass: Pass, wav_path: Path) -> tuple[str, int]:
    """rtl_sdr raw u8 IQ -> sox 2-channel int16 wav (for gr-satellites --iq)."""
    cap = config.capture
    rate = sat_pass.samplerate or 250000   # RTL min is ~250k
    freq_hz = int(round(sat_pass.freq_mhz * 1e6))
    cmd = (
        f"rtl_sdr -f {freq_hz} -s {rate} -d {cap.device_index} "
        f"-g {cap.gain} -p {cap.ppm} - "
        f"| sox -t raw -e unsigned -b 8 -c 2 -r {rate} - "
        f'-e signed -b 16 -c 2 "{wav_path}"'
    )
    return cmd, rate


def _record_iq(
    config: Config, sat_pass: Pass, stop_at: datetime, result: CaptureResult
) -> CaptureResult:
    wav_path = config.resolve_output_dir() / f"{_basename(sat_pass)}_iq.wav"
    result.wav_path = wav_path
    cmd, rate = _build_rtl_sdr_iq(config, sat_pass, wav_path)
    result.samplerate = rate
    log.info("recording %s (IQ %dsps) -> %s", sat_pass.satellite, rate, wav_path.name)
    _run_until(cmd, stop_at, result, wav_path, "rtl_sdr")
    return result


def _find_images(out_dir: Path) -> list[Path]:
    return sorted(
        p for p in out_dir.rglob("*")
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}
    )


def decode_file(
    wav_path: Path,
    out_dir: Optional[Path] = None,
    satellite_number: Optional[int] = None,
    decoder: str = "noaa_apt",
) -> tuple[list[Path], Optional[str]]:
    """Decode an already-recorded FM-demodulated APT *audio* wav with aptdec.

    Returns ``(image_paths, error)``. Used by ``run.py decode`` to (re)process a
    wav that the live pipeline didn't produce — e.g. an old ``rtl_fm`` capture.
    """
    wav = Path(wav_path)
    if not wav.exists():
        return [], f"file not found: {wav}"

    out = Path(out_dir) if out_dir else wav.parent / f"{wav.stem}_decoded"
    out.mkdir(parents=True, exist_ok=True)

    if decoder != "noaa_apt":
        return [], f"standalone decode only supports noaa_apt audio wavs (got '{decoder}')"

    out_png = out / f"{wav.stem}.png"
    cmd = _apt_audio_cmd(wav, out_png)
    if cmd is None:
        return [], (
            "no APT audio decoder found — install 'noaa-apt' (prebuilt binary, "
            "https://github.com/martinber/noaa-apt/releases) or build 'aptdec'. "
            "SatDump v2 cannot decode FM-demod audio; it needs IQ baseband or a live SDR."
        )

    log.info("decoding %s -> %s", wav.name, out)
    log.info("cmd: %s", cmd)
    try:
        subprocess.run(cmd, shell=True, check=True, timeout=300)
    except Exception as exc:  # noqa: BLE001
        return _find_images(out), f"aptdec failed: {exc}"
    return _find_images(out), None


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _simulate_record(result: CaptureResult, stop_at: datetime) -> CaptureResult:
    remaining = max(0.0, (stop_at - datetime.now(timezone.utc)).total_seconds())
    # Don't actually burn the whole pass when simulating; cap the sleep.
    sim = min(remaining, 3.0)
    log.info(
        "[dry-run] would record %s for ~%.0fs (simulating %.1fs)",
        result.sat_pass.satellite, remaining, sim,
    )
    time.sleep(sim)
    assert result.wav_path is not None
    result.wav_path.write_text(
        f"DRY-RUN capture stub for {result.sat_pass.satellite} @ "
        f"{result.sat_pass.freq_mhz} MHz, AOS {result.sat_pass.aos.isoformat()}\n",
        encoding="utf-8",
    )
    result.stopped_at = datetime.now(timezone.utc)
    result.snr_db = None
    result.ok = True
    return result


# --- SNR estimate ---------------------------------------------------------

def estimate_snr(wav_path: Path) -> Optional[float]:
    """Rough in-band SNR proxy (dB) from the recorded audio.

    Compares the strong spectral components (signal) to the median noise
    floor. Not lab-grade, but a consistent quality trend per pass/elevation.
    """
    try:
        import numpy as np

        with wave.open(str(wav_path), "rb") as w:
            n = w.getnframes()
            if n == 0:
                return None
            raw = w.readframes(min(n, 11025 * 60))  # cap at ~60s of audio
            data = np.frombuffer(raw, dtype=np.int16).astype(np.float64)

        if data.size < 1024:
            return None
        data -= data.mean()
        spec = np.abs(np.fft.rfft(data * np.hanning(data.size)))
        spec = spec[spec > 0]
        if spec.size == 0:
            return None
        noise = np.median(spec)
        signal_level = np.percentile(spec, 99)
        if noise <= 0:
            return None
        return round(20.0 * float(np.log10(signal_level / noise)), 2)
    except Exception as exc:  # noqa: BLE001
        log.debug("snr estimate failed: %s", exc)
        return None


# --- decode ---------------------------------------------------------------

def _apt_audio_cmd(wav: Path, out_png: Path) -> Optional[str]:
    """Decode an FM-demodulated APT *audio* wav into a PNG (noaa-apt/aptdec)."""
    if have("noaa-apt"):
        return f'noaa-apt "{wav}" -o "{out_png}"'
    if have("aptdec"):
        return f'aptdec -o "{out_png}" "{wav}"'
    return None


def _run(cmd: str, timeout: int) -> tuple[int, str]:
    """Run a shell command, capturing combined output. Returns (rc, output)."""
    log.info("cmd: %s", cmd)
    r = subprocess.run(cmd, shell=True, timeout=timeout,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return r.returncode, r.stdout.decode(errors="ignore")


def decode(config: Config, result: CaptureResult) -> CaptureResult:
    if not result.ok or not config.capture.decode:
        return result
    if result.decoded:  # satdump_live already produced products
        return result

    decoder = result.sat_pass.decoder
    out_dir = config.resolve_output_dir() / _basename(result.sat_pass)
    out_dir.mkdir(parents=True, exist_ok=True)

    if result.dry_run:
        stub = out_dir / "decoded_DRYRUN.txt"
        stub.write_text(f"[dry-run] would decode with '{decoder}'\n", encoding="utf-8")
        result.image_paths = [stub]
        return result

    has_wav = result.wav_path is not None and result.wav_path.exists()
    has_raw = result.raw_path is not None and result.raw_path.exists()
    if not has_wav and not has_raw:
        return result

    try:
        if decoder == "aprs":
            if not has_wav:
                result.error = "APRS decode needs FM audio wav"
                return result
            _decode_aprs(result, out_dir)
        elif decoder == "sstv":
            if not has_wav:
                result.error = "SSTV decode needs FM audio wav"
                return result
            _decode_sstv(result, out_dir)
        elif decoder == "gr_satellites":
            if not has_wav:
                result.error = "gr_satellites decode needs audio/IQ wav"
                return result
            _decode_gr_satellites(result, out_dir)
        elif decoder in ("satdump", "noaa_apt"):
            if has_raw:
                images, _, err = decode_meteor_cu8(
                    result.raw_path, out_dir, result.sat_pass,
                    samplerate=result.samplerate, config=config,
                )
                result.image_paths = images
                if images:
                    result.decoded = True
                    result.ok = True
                if err and not images:
                    result.error = err
            elif has_wav and not result.decoded:
                _decode_apt_audio(result, out_dir)
        else:  # "fm"/voice/beacon — nothing to decode, archive the wav
            log.info("archived %s recording (%s) — no decoder for mode '%s'",
                     result.sat_pass.satellite,
                     result.wav_path.name if result.wav_path else result.raw_path.name,
                     decoder)
    except Exception as exc:  # noqa: BLE001
        result.error = f"decode failed: {exc}"
        log.error(result.error)

    return result


def decode_aprs_wav(
    wav_path: Path,
    out_dir: Optional[Path] = None,
) -> tuple[list[str], Path, Optional[str]]:
    """Decode AX.25/APRS from a 22050 Hz mono wav using direwolf ``atest``.

    Returns ``(packet_lines, log_path, error)``.
    """
    wav = Path(wav_path)
    if not wav.exists():
        return [], wav, f"file not found: {wav}"
    if not have("atest"):
        return [], wav, "no APRS decoder — install 'direwolf' (provides `atest`)"

    out_dir = Path(out_dir) if out_dir else wav.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"{wav.stem}_packets.txt"

    attempts = [
        f'atest -B 1200 -F 1 "{wav}"',
        f'atest -B 1200 -P E- -F 1 "{wav}"',
        f'atest -B 1200 -P E+ -F 1 "{wav}"',
    ]
    best_rc, best_out = 1, ""
    for cmd in attempts:
        rc, out = _run(cmd, timeout=300)
        packets = _aprs_packet_lines(out)
        if packets:
            log_path.write_text(out, encoding="utf-8")
            return packets, log_path, None
        if rc == 0 or len(out) > len(best_out):
            best_rc, best_out = rc, out

    log_path.write_text(best_out, encoding="utf-8")
    err = None
    if best_rc != 0:
        err = f"atest exited {best_rc} — see {log_path.name}"
    elif not _aprs_packet_lines(best_out):
        err = "no APRS packets found (weak pass, wrong mode, or empty recording)"
    return _aprs_packet_lines(best_out), log_path, err


def _aprs_packet_lines(text: str) -> list[str]:
    """Heuristic: AX.25 callsign lines look like ``CALL>...`` with ``[`` digi path."""
    return [ln for ln in text.splitlines() if "[" in ln and "]" in ln and ">" in ln]


def _decode_aprs(result: CaptureResult, out_dir: Path) -> None:
    """Decode AX.25/APRS packets from the wav using direwolf's `atest`."""
    wav = result.wav_path
    assert wav is not None
    packets, log_path, err = decode_aprs_wav(wav, out_dir)
    result.image_paths = [log_path]
    log.info("APRS: decoded %d packet line(s) from %s -> %s",
             len(packets), result.sat_pass.satellite, log_path.name)
    if err:
        result.error = err
        log.warning(result.error)


def _decode_sstv(result: CaptureResult, out_dir: Path) -> None:
    """Decode an SSTV transmission from the wav (pysstv `sstv` CLI) if available."""
    wav = result.wav_path
    assert wav is not None
    out_png = out_dir / f"{wav.stem}.png"
    if have("sstv"):
        _run(f'sstv -d "{wav}" -o "{out_png}"', timeout=600)
        result.image_paths = _find_images(out_dir)
    if not result.image_paths:
        log.warning("SSTV: no decoder output for %s (wav archived at %s). "
                    "Install pysstv (`pip install sstv`) or decode in QSSTV.",
                    result.sat_pass.satellite, wav)


def _decode_gr_satellites(result: CaptureResult, out_dir: Path) -> None:
    """Decode amateur-sat telemetry frames with gr-satellites."""
    sp = result.sat_pass
    wav = result.wav_path
    assert wav is not None
    if not gr_satellites_cmd():
        result.error = (
            "gr_satellites not available — build from source and run "
            "scripts/fix_gr_satellites.sh (see README)"
        )
        log.warning(result.error)
        return

    name = sp.gr_name or sp.satellite
    is_iq = sp.mode == "iq"
    samp = result.samplerate or sp.samplerate or (250000 if is_iq else 48000)
    kiss = out_dir / f"{wav.stem}.kiss"
    decoded_txt = out_dir / f"{wav.stem}_telemetry.txt"
    gr = gr_satellites_cmd()

    cmd = (
        f'{gr} "{name}" --wavfile "{wav}" --samp_rate {samp} '
        f'{"--iq " if is_iq else ""}--kiss_out "{kiss}" --hexdump'
    )
    rc, out = _run(cmd, timeout=900)
    decoded_txt.write_text(out, encoding="utf-8")

    frames = kiss.stat().st_size if kiss.exists() else 0
    artifacts = [p for p in (kiss, decoded_txt) if p.exists()]
    result.image_paths = artifacts
    if frames > 0:
        log.info("gr-satellites: decoded telemetry from %s (%d bytes of frames) -> %s",
                 sp.satellite, frames, kiss.name)
    else:
        log.warning("gr-satellites: no frames decoded for %s (weak pass?). Log: %s",
                    sp.satellite, decoded_txt.name)


def _meteor_modulation(sat_pass: Pass) -> str:
    """LRPT modulation for Meteor-M series."""
    sat = _satdump_satellite_number(sat_pass) or ""
    if sat in ("M2-2", "M2-3", "M2-4"):
        return "oqpsk"
    return "oqpsk" if "M2" in sat_pass.satellite.upper() else "qpsk"


def _cu8_recording_info(raw: Path, samplerate: int) -> tuple[float, Optional[str]]:
    """Return (duration_seconds, error_if_invalid). cu8 = uint8 I/Q interleaved (2 bytes/sample)."""
    size = raw.stat().st_size
    if size < 1024:
        return 0.0, f"IQ file too small ({size} bytes) — recording likely failed"
    duration = size / (2.0 * samplerate)
    if duration < 30.0:
        return duration, f"IQ file only ~{duration:.0f}s long — pass may be too short to decode"
    return duration, None


# ~1s of LRPT symbols at 72k sym/s — smaller .s files are failed/empty demods.
_MIN_METEOR_SYM_BYTES = 50_000


def _decode_meteor_demod(
    raw: Path,
    out_dir: Path,
    sat_pass: Pass,
    samplerate: int,
    log_path: Path,
) -> tuple[list[Path], Optional[str]]:
    """Fallback METEOR decode: meteor_demod (cu8) -> meteor_decode. No SatDump required."""
    if not have("meteor_demod"):
        return [], "meteor_demod not installed (build from github.com/dbdexter-dev/meteor_demod)"
    mod = _meteor_modulation(sat_pass)
    sym = out_dir / f"{raw.stem}.s"
    prefix = out_dir / "meteor_lrpt"

    if sym.exists() and sym.stat().st_size >= _MIN_METEOR_SYM_BYTES:
        log.info("reusing existing symbol file %s (%d bytes)", sym.name, sym.stat().st_size)
        log_chunk = f"# reusing {sym}\n"
    else:
        if sym.exists():
            log.warning(
                "discarding tiny symbol file %s (%d bytes) — re-running demod",
                sym.name, sym.stat().st_size,
            )
            sym.unlink(missing_ok=True)
        demod_cmd = (
            f'meteor_demod --bps 8 -s {samplerate} -m {mod} -r 72000 '
            f'-B -q -o "{sym}" "{raw}"'
        )
        rc, out = _run(demod_cmd, timeout=1800)
        log_chunk = f"$ {demod_cmd}\n(exit {rc})\n{out}\n"
        if rc != 0 or not sym.exists() or sym.stat().st_size == 0:
            if log_path.exists():
                log_path.write_text(
                    log_path.read_text(encoding="utf-8") + "\n--- meteor_demod ---\n" + log_chunk,
                    encoding="utf-8",
                )
            else:
                log_path.write_text(log_chunk, encoding="utf-8")
            return [], f"meteor_demod failed (rc={rc}) — see {log_path.name}"
        if sym.stat().st_size < _MIN_METEOR_SYM_BYTES:
            if log_path.exists():
                log_path.write_text(
                    log_path.read_text(encoding="utf-8") + "\n--- meteor_demod ---\n" + log_chunk,
                    encoding="utf-8",
                )
            else:
                log_path.write_text(log_chunk, encoding="utf-8")
            return [], (
                f"meteor_demod found no usable signal ({sym.stat().st_size} byte .s, "
                f"need >{_MIN_METEOR_SYM_BYTES}) — weak pass or wrong freq/sample rate"
            )

    decode_cmds: list[str] = []
    if have("meteor_decode"):
        img = out_dir / f"{raw.stem}_meteor.png"
        decode_cmds.append(f'meteor_decode "{sym}" -diff -o "{img}"')
    elif have("medet"):
        decode_cmds.append(f'medet -diff -r 65 -g 65 -b 64 "{sym}" "{prefix}"')

    if not decode_cmds:
        msg = (
            f"meteor_demod OK ({sym.stat().st_size} byte symbol file) but meteor_decode "
            f"not installed — run: bash scripts/install_meteor_demod.sh"
        )
        if log_path.exists():
            log_path.write_text(log_path.read_text(encoding="utf-8") + "\n--- meteor_demod ---\n" + log_chunk,
                                encoding="utf-8")
        else:
            log_path.write_text(log_chunk, encoding="utf-8")
        return [], msg

    combined = log_chunk
    if log_path.exists():
        combined = log_path.read_text(encoding="utf-8") + "\n--- meteor_demod ---\n" + log_chunk
    for cmd in decode_cmds:
        rc, dout = _run(cmd, timeout=600)
        combined += f"$ {cmd}\n(exit {rc})\n{dout}\n"

    log_path.write_text(combined, encoding="utf-8")
    images = _find_images(out_dir)
    if images:
        log.info("meteor_demod: %d image(s) from %s", len(images), sat_pass.satellite)
        return images, None
    return [], (
        f"meteor_decode produced no images (weak pass or no signal lock?) — "
        f"symbols at {sym.name} ({sym.stat().st_size} bytes)"
    )


def decode_meteor_cu8(
    raw_path: Path,
    out_dir: Path,
    sat_pass: Pass,
    *,
    samplerate: Optional[int] = None,
    config: Optional[Config] = None,
) -> tuple[list[Path], Path, Optional[str]]:
    """Decode METEOR LRPT from an rtl_sdr ``.cu8`` recording.

    Uses meteor_demod when SatDump is broken; otherwise SatDump first, then fallback.

    Returns ``(image_paths, log_path, error)``.
    """
    raw = Path(raw_path).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "satdump_offline.log"

    if not raw.exists():
        return [], log_path, f"file not found: {raw}"

    rate = samplerate or sat_pass.samplerate
    if rate is None and config is not None:
        rate = config.capture.live_sample_rate
    if rate is None:
        rate = 1024000

    duration, cu8_err = _cu8_recording_info(raw, rate)
    if cu8_err:
        log.warning("METEOR IQ: %s (%s, %.1f MB)", cu8_err, raw.name, raw.stat().st_size / 1e6)
        if duration < 5.0:
            return [], log_path, cu8_err

    sd_ok, sd_msg = satdump_smoke_test() if have("satdump") else (False, "not installed")
    err: Optional[str] = None

    # SatDump offline when the binary actually starts (plugin fix applied).
    if have("satdump") and sd_ok:
        log.info("decoding %s via SatDump offline (%.0fs IQ, %d sps)",
                 sat_pass.satellite, duration, rate)
        images, log_path, err = decode_satdump_raw(
            raw, out_dir, sat_pass, samplerate=rate, config=config,
        )
        if images:
            return images, log_path, None

    if have("meteor_demod"):
        log.info("decoding %s via meteor_demod (%.0fs IQ, %d sps)",
                 sat_pass.satellite, duration, rate)
        images, err = _decode_meteor_demod(raw, out_dir, sat_pass, rate, log_path)
        if images:
            return images, log_path, None
    elif not have("satdump"):
        err = "no METEOR decoder — bash scripts/install_meteor_demod.sh"
    elif not sd_ok:
        err = sd_msg

    if err and not have("meteor_demod") and have("satdump") and not sd_ok:
        err += " | sudo bash scripts/fix_satdump_plugins.sh"

    return [], log_path, err or "decode produced no images"


def decode_satdump_raw(
    raw_path: Path,
    out_dir: Path,
    sat_pass: Pass,
    *,
    samplerate: Optional[int] = None,
    config: Optional[Config] = None,
) -> tuple[list[Path], Path, Optional[str]]:
    """Decode a cu8 IQ recording with SatDump offline.

    Returns ``(image_paths, log_path, error)``.
    """
    raw = Path(raw_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "satdump_offline.log"

    if not raw.exists():
        return [], log_path, f"file not found: {raw}"
    if not have("satdump"):
        return [], log_path, "satdump not installed"

    pipeline = _satdump_pipeline(sat_pass)
    rate = samplerate or sat_pass.samplerate
    if rate is None and config is not None:
        rate = config.capture.live_sample_rate
    if rate is None:
        rate = 1024000

    cmds = _satdump_offline_commands(pipeline, raw, out_dir, rate, sat_pass)
    log.info("satdump offline decode: %s (%s, %d sps)", sat_pass.satellite, raw.name, rate)

    combined: list[str] = []
    plugin_crash = False
    for cmd in cmds:
        rc, out = _run(cmd, timeout=900)
        combined.append(f"$ {cmd}\n(exit {rc})\n{out}\n")
        if satdump_plugin_crash(out, rc):
            plugin_crash = True
            break
        images = _find_images(out_dir)
        if images:
            log_path.write_text("\n".join(combined), encoding="utf-8")
            log.info("satdump offline: %d image(s) from %s", len(images), sat_pass.satellite)
            return images, log_path, None

    log_path.write_text("\n".join(combined), encoding="utf-8")
    if plugin_crash:
        err = (
            "SatDump plugin crash (exit 134) — run: sudo bash scripts/fix_satdump_plugins.sh "
            "or install SatDump v2: bash scripts/build_satdump.sh"
        )
    else:
        err = "satdump offline produced no images"
        if combined:
            last = combined[-1]
            if "(exit " in last:
                err += f" (last {last.split('(exit ')[1].split(')')[0]})"
    log.warning("%s — see %s", err, log_path.name)
    return [], log_path, err


def _decode_satdump_offline(config: Config, result: CaptureResult, out_dir: Path) -> None:
    """Decode a recorded cu8 IQ file with SatDump (works on v1.x and v2.x)."""
    raw = result.raw_path
    assert raw is not None
    images, _, err = decode_meteor_cu8(
        raw, out_dir, result.sat_pass,
        samplerate=result.samplerate, config=config,
    )
    result.image_paths = images
    if images:
        result.decoded = True
        result.ok = True
    elif err:
        result.error = err


def _decode_apt_audio(result: CaptureResult, out_dir: Path) -> None:
    wav = result.wav_path
    assert wav is not None
    cmd = _apt_audio_cmd(wav, out_dir / f"{wav.stem}.png")
    if cmd is None:
        result.error = "no APT audio decoder (install noaa-apt) and satdump unavailable"
        log.warning(result.error)
        return
    rc, _ = _run(cmd, timeout=300)
    result.image_paths = _find_images(out_dir)
    if not result.image_paths:
        log.warning("APT decoder produced no images for %s", result.sat_pass.satellite)
