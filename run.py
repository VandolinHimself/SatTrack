#!/usr/bin/env python3
"""SatTrack — single entry point.

Just run::

    python run.py            # dashboard + auto-monitoring + live TLE updates

Other handy subcommands::

    python run.py passes     # show the next 24h of passes (no capture)
    python run.py tle        # refresh TLEs and print the registry
    python run.py doppler "NOAA 19"   # Doppler curve for its next pass
    python run.py stats      # telemetry summary from the SQLite DB
    python run.py serve      # web dashboard only (no RTL capture)
    python run.py watch      # RTL capture daemon only (no dashboard)

On Windows / machines without an RTL-SDR this runs in dry-run mode: it does
all the prediction + scheduling and stubs the capture, so you can develop
anywhere and deploy the exact same code on your Kali ground station.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

# NOTE: heavy imports (skyfield/numpy via sattrack.predict) are done lazily
# inside each command so `doctor` can still report missing deps instead of
# crashing at import time.
from sattrack.config import load_config
from sattrack.tle import data_dir


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=getattr(logging, level.upper(), logging.INFO),
    )


def _local(dt: datetime) -> str:
    return dt.astimezone().strftime("%a %Y-%m-%d %H:%M:%S")


def cmd_run(args) -> int:
    """Dashboard + watcher in one process — TLEs refresh automatically."""
    import threading

    import uvicorn

    from sattrack.web.app import create_app
    from sattrack.watcher import Watcher

    config = load_config(args.config)
    if getattr(args, "hours", None):
        config.prediction.horizon_hours = args.hours

    host = getattr(args, "host", None) or "0.0.0.0"
    port = getattr(args, "port", None) or 8082
    no_web = getattr(args, "no_web", False)
    no_watch = getattr(args, "no_watch", False)

    web_thread: threading.Thread | None = None
    if not no_web:
        app = create_app(args.config)

        def _run_web() -> None:
            uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower())

        web_thread = threading.Thread(target=_run_web, name="sattrack-web", daemon=True)
        web_thread.start()
        print(f"\nSatTrack — dashboard http://{host}:{port}/")
        print("  WebSocket live feed at /ws")
        print(f"  TLE refresh every {config.prediction.tle_refresh_interval_hours:.0f}h from Celestrak")
        if not no_watch:
            print("  RTL capture daemon running in this process\n")
        else:
            print("  Dashboard only (no RTL capture)\n")

    if not no_watch:
        Watcher(config).run()
    elif web_thread is not None:
        web_thread.join()
    return 0


def cmd_watch(args) -> int:
    args.no_web = True
    args.no_watch = False
    return cmd_run(args)


def cmd_passes(args) -> int:
    from sattrack.predict import predict_all
    from sattrack.registry import build_registry
    from sattrack.watcher import _capture_window, _key, plan_schedule

    config = load_config(args.config)
    registry = build_registry(config, refresh=not args.offline)
    passes = predict_all(config, registry, hours=args.hours or config.prediction.horizon_hours)
    if not passes:
        print("No passes predicted in the window.")
        return 0

    planned, skipped = plan_schedule(passes, config)
    planned_keys = {_key(p) for p in planned}

    print(f"\nUpcoming passes for {config.observer.name} "
          f"(>= {config.prediction.min_elevation_deg:.0f} deg):\n")
    print(f"{'AOS (local)':<26}{'Sat':<14}{'MaxEl':>6}{'Dur':>6}{'Freq(MHz)':>12}  {'Status':<22} Dir")
    print("-" * 100)
    for p in passes:
        pk = _key(p)
        if pk in planned_keys:
            w0, w1 = _capture_window(p, config)
            status = f"SCHEDULED {w0.astimezone().strftime('%H:%M')}-{w1.astimezone().strftime('%H:%M')}"
        elif pk in skipped:
            # Find which scheduled pass blocked this one.
            blocker = next(
                (s for s in planned if _capture_window(p, config)[0] < _capture_window(s, config)[1]
                 and _capture_window(s, config)[0] < _capture_window(p, config)[1]),
                None,
            )
            status = f"SKIP (overlaps {blocker.satellite})" if blocker else "SKIP (overlap)"
        else:
            status = "?"
        print(f"{_local(p.aos):<26}{p.satellite:<14}{p.max_elevation_deg:>5.0f} "
              f"{p.duration_s:>5.0f}s{p.freq_mhz:>11.4f}  {status:<22} {p.direction}")

    n_sched = len([p for p in passes if _key(p) in planned_keys and p.los > datetime.now(timezone.utc)])
    print(f"\n{len(passes)} predicted, {n_sched} scheduled for capture (overlaps resolved).")
    print(f"DB: {data_dir() / 'sattrack.db'}")
    return 0


def cmd_tle(args) -> int:
    from sattrack.registry import build_registry, registry_to_json

    config = load_config(args.config)
    registry = build_registry(config, refresh=not args.offline)
    print(registry_to_json(registry))
    print(f"\nTLE store: {data_dir() / 'custom.tle'}", file=sys.stderr)
    return 0


def cmd_tle_refresh(args) -> int:
    from sattrack.registry import build_registry, registry_has_stale_tles
    from sattrack.tle import data_dir, update_tles

    config = load_config(args.config)
    ids = config.norad_ids
    print(f"Fetching TLEs for {len(ids)} satellites from Celestrak ...")
    tles = update_tles(ids)
    print(f"  got {len(tles)}/{len(ids)} — stored in {data_dir() / 'custom.tle'}")
    registry = build_registry(config, refresh=False)  # use cache we just wrote
    stale = registry_has_stale_tles(registry, config.prediction.tle_max_age_hours)
    for name, entry in registry.items():
        age = entry.tle.age_hours
        age_s = f"{age:.1f}h" if age is not None else "?"
        flag = " (epoch old — Celestrak has no newer elements)" if (
            age is not None and age > config.prediction.tle_max_age_hours
        ) else ""
        print(f"  {name:<16} epoch age {age_s}{flag}")
    if stale:
        print("\nNote: some epochs are still > limit — that means the operator "
              "hasn't published newer elements yet, not that we skipped fetching.")
    return 0


def cmd_doppler(args) -> int:
    from sattrack.predict import doppler_curve, predict_all
    from sattrack.registry import build_registry

    config = load_config(args.config)
    registry = build_registry(config, refresh=not args.offline)
    entry = registry.get(args.satellite)
    if entry is None:
        print(f"Unknown/!available satellite: {args.satellite}", file=sys.stderr)
        print("Available:", ", ".join(registry.keys()), file=sys.stderr)
        return 1
    passes = predict_all(config, {entry.name: entry}, hours=args.hours or 24)
    if not passes:
        print("No upcoming pass to compute Doppler for.")
        return 0
    p = passes[0]
    print(f"\nDoppler for {p.satellite} next pass — AOS {_local(p.aos)}, "
          f"max el {p.max_elevation_deg:.0f} deg, {p.freq_mhz:.4f} MHz\n")
    print(f"{'time(local)':<12}{'el':>6}{'range(km)':>11}{'doppler(Hz)':>13}{'tune(MHz)':>13}")
    print("-" * 56)
    for s in doppler_curve(config, entry, p, samples=20):
        t = datetime.fromisoformat(s["t"]).astimezone().strftime("%H:%M:%S")
        print(f"{t:<12}{s['elevation_deg']:>6.1f}{s['range_km']:>11.0f}"
              f"{s['doppler_hz']:>13.0f}{s['corrected_freq_hz']/1e6:>13.5f}")
    return 0


def cmd_decode_aprs(args) -> int:
    from pathlib import Path

    from sattrack.capture import decode_aprs_wav, have

    config = load_config(args.config)
    wav = Path(args.wavfile)
    if not wav.exists():
        candidate = config.resolve_output_dir() / args.wavfile
        if candidate.exists():
            wav = candidate
        else:
            # Allow passing a capture folder — pick the .wav inside.
            folder = config.resolve_output_dir() / args.wavfile
            if folder.is_dir():
                wavs = sorted(folder.glob("*.wav"))
                if len(wavs) == 1:
                    wav = wavs[0]
                elif wavs:
                    print("error: folder has multiple wavs — pass the file explicitly:", file=sys.stderr)
                    for w in wavs:
                        print(f"  {w}", file=sys.stderr)
                    return 2
    if not wav.exists():
        print(f"error: wav not found: {args.wavfile}", file=sys.stderr)
        print(f"  (also looked in {config.resolve_output_dir()})", file=sys.stderr)
        return 2
    if not have("atest"):
        print("error: atest not found — install direwolf (apt install direwolf)", file=sys.stderr)
        return 2

    print(f"Decoding APRS from {wav} ...")
    packets, log_path, error = decode_aprs_wav(wav)
    if error and not packets:
        print(f"\n[FAILED] {error}", file=sys.stderr)
        print(f"  full log: {log_path}", file=sys.stderr)
        return 1

    print(f"\n[OK] {len(packets)} packet(s) — log: {log_path}\n")
    show = packets if args.all else packets[:20]
    for ln in show:
        print(ln)
    if not args.all and len(packets) > 20:
        print(f"... and {len(packets) - 20} more (use --all)")
    if error:
        print(f"\n[WARN] {error}", file=sys.stderr)
    return 0


def cmd_decode_meteor(args) -> int:
    from pathlib import Path

    from sattrack.capture import decode_meteor_cu8, have, satdump_smoke_test
    from sattrack.predict import pass_from_satellite

    config = load_config(args.config)
    target = Path(args.target)
    cap_dir = config.resolve_output_dir()

    if not target.exists():
        candidate = cap_dir / args.target
        if candidate.exists():
            target = candidate

    raw: Path | None = None
    out_dir: Path | None = None

    if target.is_dir():
        cu8s = sorted(target.glob("*.cu8"))
        if len(cu8s) == 1:
            raw = cu8s[0]
            out_dir = target
        elif cu8s:
            print("error: folder has multiple .cu8 files — pass one explicitly:", file=sys.stderr)
            for c in cu8s:
                print(f"  {c}", file=sys.stderr)
            return 2
    elif target.suffix.lower() == ".cu8":
        raw = target
        out_dir = target.parent

    if raw is None or not raw.exists():
        print(f"error: .cu8 not found: {args.target}", file=sys.stderr)
        print(f"  (also looked in {cap_dir})", file=sys.stderr)
        return 2
    if out_dir is None:
        out_dir = raw.parent

    sat = config.satellite_by_name(args.satellite) if args.satellite else None
    if sat is None:
        hint = (raw.parent.name + raw.name).upper()
        if "METEOR" in hint:
            sat = next((s for s in config.enabled_satellites if "METEOR" in s.name.upper()), None)
        if sat is None:
            sat = next((s for s in config.enabled_satellites if s.decoder in ("satdump", "noaa_apt")), None)
    if sat is None:
        print("error: no METEOR/satdump target in config — use --satellite", file=sys.stderr)
        return 2

    sat_pass = pass_from_satellite(sat)
    if args.pipeline:
        sat_pass.pipeline = args.pipeline
    if args.samplerate:
        sat_pass.samplerate = args.samplerate
    if args.satellite_number:
        sat_pass.satdump_satellite_number = args.satellite_number

    rate = sat_pass.samplerate or config.capture.live_sample_rate
    print(f"Decoding METEOR LRPT from {raw.name} ({rate} sps, pipeline={sat_pass.pipeline}) ...")

    if have("satdump"):
        ok, msg = satdump_smoke_test()
        if not ok:
            print(f"  [warn] SatDump: {msg}", file=sys.stderr)

    images, log_path, error = decode_meteor_cu8(
        raw, out_dir, sat_pass, samplerate=rate, config=config,
    )
    if error and not images:
        print(f"\n[FAILED] {error}", file=sys.stderr)
        print(f"  log: {log_path}", file=sys.stderr)
        return 1

    print(f"\n[OK] {len(images)} image(s) — log: {log_path}")
    for p in images[:20]:
        print(f"  {p}")
    if len(images) > 20:
        print(f"  ... and {len(images) - 20} more")
    if error:
        print(f"\n[WARN] {error}", file=sys.stderr)
    return 0


cmd_decode_satdump = cmd_decode_meteor


def cmd_decode(args) -> int:
    from pathlib import Path

    from sattrack.capture import decode_file

    config = load_config(args.config)

    # Resolve the wav: as given, then relative to the captures output dir.
    wav = Path(args.wavfile)
    if not wav.exists():
        candidate = config.resolve_output_dir() / args.wavfile
        if candidate.exists():
            wav = candidate
    if not wav.exists():
        print(f"error: wav not found: {args.wavfile}", file=sys.stderr)
        print(f"  (also looked in {config.resolve_output_dir()})", file=sys.stderr)
        return 2

    print(f"Decoding {wav} ...")
    images, error = decode_file(wav, satellite_number=args.sat)
    if error:
        print(f"\n[FAILED] {error}", file=sys.stderr)
        if images:
            print("  partial images:")
            for p in images:
                print(f"    {p}")
        return 1

    if not images:
        print("\n[WARN] decoder ran but produced no images (weak/incomplete recording?)")
        return 1
    print(f"\n[OK] {len(images)} image(s):")
    for p in images:
        print(f"  {p}")
    return 0


def cmd_stats(args) -> int:
    from sattrack.telemetry import Telemetry

    config = load_config(args.config)
    db = data_dir() / "sattrack.db"
    with Telemetry(db) as t:
        s = t.stats()
        print(f"\nSatTrack telemetry — {db}\n")
        for k, v in s.items():
            print(f"  {k:<22}: {v}")
        recent = t.recent_captures(10)
        if recent:
            print("\n  recent captures:")
            for r in recent:
                print(f"    {r['aos']}  {r['satellite']:<12} "
                      f"score={r['quality_score']} ok={r['ok']} imgs={r['image_count']}")
    return 0


def cmd_serve(args) -> int:
    args.no_web = False
    args.no_watch = True
    return cmd_run(args)


def cmd_doctor(args) -> int:
    from sattrack.capture import (
        gr_satellites_available,
        have,
        hardware_available,
        preview_satdump_live_command,
        preview_satdump_offline_command,
        resolve_dry_run,
        satdump_smoke_test,
        satdump_version,
        select_backend,
        use_satdump_offline,
    )
    from sattrack.predict import pass_from_satellite

    config = load_config(args.config)
    print("SatTrack environment check")
    print(f"  config            : {config.path}")
    print(f"  observer          : {config.observer.name} "
          f"({config.observer.latitude}, {config.observer.longitude})")
    print(f"  satellites        : {len(config.enabled_satellites)} enabled")
    print(f"  SDR hardware ready : {hardware_available()}")
    print(f"  effective dry_run  : {resolve_dry_run(config)}")
    print(f"  capture backend    : {select_backend(config)}")
    sdv = satdump_version()
    sd_mode = "offline (IQ record)" if use_satdump_offline(config) else "live"
    print(f"  satdump            : v{sdv[0]}.{sdv[1]}.{sdv[2]} mode={sd_mode}" if sdv else "  satdump            : not found")
    if have("satdump"):
        sd_ok, sd_msg = satdump_smoke_test()
        print(f"  satdump smoke test : {'OK' if sd_ok else sd_msg}")
    print(f"  meteor_demod       : {have('meteor_demod')}  meteor_decode: {have('meteor_decode')}")
    if not have("meteor_demod"):
        print("                       install: bash install.sh  (or install_meteor_demod.sh)")
    print(f"  tools              : satdump={have('satdump')} rtl_fm={have('rtl_fm')} "
          f"sox={have('sox')} rtl_sdr={have('rtl_sdr')}")
    print(f"  decoders           : noaa-apt={have('noaa-apt')} atest/direwolf(APRS)={have('atest')} "
          f"sstv={have('sstv')} gr_satellites={gr_satellites_available()}")
    print("\n  decode commands (sanity-check syntax):")
    for sat in config.enabled_satellites:
        if sat.decoder == "aprs":
            print(f"    {sat.name:<16} atest -B 1200 -F 1 <recording.wav>")
        elif sat.decoder == "fm":
            print(f"    {sat.name:<16} (archive only — no decode)")
        elif sat.decoder in ("satdump", "noaa_apt"):
            p = pass_from_satellite(sat)
            rate = sat.samplerate or config.capture.live_sample_rate
            if use_satdump_offline(config):
                print(f"    {sat.name:<16} rtl_sdr -f {int(sat.freq_mhz*1e6)} -s {rate} -g {config.capture.gain} capture.cu8")
                print(f"    {'':16} {preview_satdump_offline_command(config, p)}")
            else:
                print(f"    {sat.name:<16} {preview_satdump_live_command(config, p)}")
        elif sat.decoder == "gr_satellites":
            print(f"    {sat.name:<16} gr_satellites (disabled/broken on this host)")
    share = config.sdr_sharing
    if share.enabled:
        print(f"  sdr sharing        : ON  release='{share.release_command}' "
              f"reacquire='{share.reacquire_command}' settle={share.settle_seconds}s")
        if share.watchdog:
            from sattrack.capture import consumer_is_up
            up = consumer_is_up(config)
            state = {True: "up", False: "DOWN", None: "unknown"}[up]
            print(f"  sdr watchdog       : ON  status='{share.status_command}' "
                  f"every {share.watchdog_interval_seconds}s  (currently: {state})")
        else:
            print("  sdr watchdog       : off")
    else:
        print("  sdr sharing        : off")
    km = config.kismet
    if km.enabled and km.url:
        from sattrack.kismet_adsb import fetch_adsb
        import shutil
        kismet_bin = shutil.which("kismet")
        if kismet_bin:
            print(f"  kismet binary       : {kismet_bin}")
        else:
            print("  kismet binary       : NOT FOUND — run: bash install.sh")
        adsb = fetch_adsb(config)
        st = "OK" if adsb.get("online") else adsb.get("error", "down")
        if adsb.get("auth_required"):
            st = "401 auth required — set kismet.password or KISMET_PASSWORD"
        n = adsb.get("count", 0)
        raw = adsb.get("raw_devices", 0)
        extra = f", {raw} raw" if raw else ""
        print(f"  kismet adsb        : {km.url} -> {n} aircraft{extra} ({st})")
    else:
        print("  kismet adsb        : disabled")
    print(f"  data dir           : {data_dir()}")
    try:
        import skyfield, sgp4, numpy  # noqa: F401
        print("  python deps        : skyfield/sgp4/numpy OK")
    except Exception as exc:  # noqa: BLE001
        print(f"  python deps        : MISSING ({exc}) -> pip install -r requirements.txt")
    try:
        import fastapi, uvicorn  # noqa: F401
        print("  web dashboard      : fastapi/uvicorn OK  (python run.py serve)")
    except Exception:
        print("  web dashboard      : not installed -> pip install -r requirements.txt")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sattrack", description="Automated satellite ground station")
    p.add_argument("-c", "--config", help="Path to config.json")
    p.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR")
    p.add_argument("--host", default="0.0.0.0", help="dashboard bind address (default 0.0.0.0)")
    p.add_argument("--port", type=int, default=8082, help="dashboard HTTP port (default 8082)")
    p.add_argument("--hours", type=float, help="prediction horizon override (hours)")
    p.add_argument("--no-web", action="store_true", help="RTL capture only, no dashboard")
    p.add_argument("--no-watch", action="store_true", help="dashboard only, no RTL capture")
    sub = p.add_subparsers(dest="command")

    w = sub.add_parser("watch", help="RTL capture daemon only (no dashboard)")
    w.add_argument("--hours", type=float, help="prediction horizon override")
    w.set_defaults(func=cmd_watch, no_web=True, no_watch=False)

    pa = sub.add_parser("passes", help="list upcoming passes")
    pa.add_argument("--hours", type=float)
    pa.add_argument("--offline", action="store_true", help="use cached TLEs only")
    pa.set_defaults(func=cmd_passes)

    tl = sub.add_parser("tle", help="refresh + print satellite registry")
    tl.add_argument("--offline", action="store_true")
    tl.set_defaults(func=cmd_tle)

    tr = sub.add_parser("tle-refresh", help="fetch latest TLEs from Celestrak now")
    tr.set_defaults(func=cmd_tle_refresh)

    dp = sub.add_parser("doppler", help="Doppler curve for a satellite's next pass")
    dp.add_argument("satellite")
    dp.add_argument("--hours", type=float)
    dp.add_argument("--offline", action="store_true")
    dp.set_defaults(func=cmd_doppler)

    dc = sub.add_parser("decode", help="decode an existing APT audio wav with aptdec")
    dc.add_argument("wavfile", help="path to a recorded .wav (or just its filename)")
    dc.add_argument("--sat", type=int, help="satellite number (15/18/19), optional")
    dc.set_defaults(func=cmd_decode)

    ap = sub.add_parser("decode-aprs", help="decode APRS/AX.25 from a recorded wav (atest)")
    ap.add_argument("wavfile", help="path to .wav, capture folder name, or filename under captures/")
    ap.add_argument("--all", action="store_true", help="print every packet line (default: first 20)")
    ap.set_defaults(func=cmd_decode_aprs)

    mt = sub.add_parser("decode-meteor", help="decode METEOR LRPT from a .cu8 capture")
    mt.add_argument("target", help=".cu8 file or capture folder under captures/")
    mt.add_argument("--satellite", help="config satellite name (default: auto-detect METEOR)")
    mt.add_argument("--pipeline", help="SatDump pipeline override, e.g. meteor_m2-x_lrpt")
    mt.add_argument("--samplerate", type=int, help="IQ sample rate in sps (default: from config)")
    mt.add_argument("--satellite-number", help="SatDump satellite id, e.g. M2-3")
    mt.set_defaults(func=cmd_decode_meteor)

    sd = sub.add_parser("decode-satdump", help="alias for decode-meteor")
    sd.add_argument("target")
    sd.add_argument("--satellite")
    sd.add_argument("--pipeline")
    sd.add_argument("--samplerate", type=int)
    sd.add_argument("--satellite-number")
    sd.set_defaults(func=cmd_decode_satdump)

    st = sub.add_parser("stats", help="telemetry summary")
    st.set_defaults(func=cmd_stats)

    sv = sub.add_parser("serve", help="web dashboard only (no RTL capture)")
    sv.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    sv.add_argument("--port", type=int, default=8082, help="HTTP port (default 8082)")
    sv.set_defaults(func=cmd_serve, no_web=False, no_watch=True)

    doc = sub.add_parser("doctor", help="environment / readiness check")
    doc.set_defaults(func=cmd_doctor)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    if not getattr(args, "command", None):
        # Bare `python run.py` => dashboard + watcher + live TLE updates.
        args.func = cmd_run
        args.no_web = getattr(args, "no_web", False)
        args.no_watch = getattr(args, "no_watch", False)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
