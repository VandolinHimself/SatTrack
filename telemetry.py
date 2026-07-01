"""SSA-style telemetry layer (SQLite).

Persists what the ground station observed so it can be trended in Grafana
(via the SQLite/Sqlite-datasource or an export to InfluxDB):

* ``passes``   — every predicted pass
* ``captures`` — recording outcome, SNR, quality score, decoded image count
* ``doppler``  — per-pass Doppler / range-rate samples

Pass-quality scoring combines peak elevation, dwell time and measured SNR
into a 0-100 figure of merit.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:  # type-only; avoids pulling skyfield for read-only commands
    from .capture import CaptureResult
    from .predict import Pass

log = logging.getLogger("sattrack.telemetry")


def compute_pass_score(max_elevation_deg: float, duration_s: float, snr_db: Optional[float]) -> float:
    """0-100 figure of merit for a pass/capture."""
    el = max(0.0, min(max_elevation_deg, 90.0)) / 90.0          # 0..1
    dwell = max(0.0, min(duration_s, 900.0)) / 900.0            # 0..1 (cap 15 min)
    geometry = 0.6 * el + 0.4 * dwell                          # weight elevation most
    if snr_db is None:
        return round(100.0 * geometry, 1)
    snr_norm = max(0.0, min(snr_db, 40.0)) / 40.0              # 0..1 (cap 40 dB)
    return round(100.0 * (0.7 * geometry + 0.3 * snr_norm), 1)


SCHEMA = """
CREATE TABLE IF NOT EXISTS passes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    satellite TEXT NOT NULL,
    norad_id INTEGER NOT NULL,
    freq_mhz REAL NOT NULL,
    decoder TEXT,
    aos TEXT NOT NULL,
    los TEXT NOT NULL,
    max_time TEXT,
    max_elevation_deg REAL,
    aos_azimuth_deg REAL,
    los_azimuth_deg REAL,
    duration_s REAL,
    direction TEXT,
    priority INTEGER,
    predicted_at TEXT NOT NULL,
    UNIQUE(norad_id, aos)
);

CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id INTEGER,
    satellite TEXT,
    aos TEXT,
    wav_path TEXT,
    image_count INTEGER DEFAULT 0,
    snr_db REAL,
    recorded_seconds REAL,
    quality_score REAL,
    dry_run INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 0,
    error TEXT,
    started_at TEXT,
    stopped_at TEXT,
    FOREIGN KEY(pass_id) REFERENCES passes(id)
);

CREATE TABLE IF NOT EXISTS doppler (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id INTEGER,
    satellite TEXT,
    t TEXT,
    elevation_deg REAL,
    range_km REAL,
    range_rate_km_s REAL,
    doppler_hz REAL,
    corrected_freq_hz REAL,
    FOREIGN KEY(pass_id) REFERENCES passes(id)
);

CREATE INDEX IF NOT EXISTS idx_passes_aos ON passes(aos);
CREATE INDEX IF NOT EXISTS idx_captures_aos ON captures(aos);
CREATE INDEX IF NOT EXISTS idx_doppler_pass ON doppler(pass_id);
"""


class Telemetry:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Telemetry":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- writes ------------------------------------------------------------

    def record_pass(self, sat_pass: Pass) -> int:
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO passes
            (satellite, norad_id, freq_mhz, decoder, aos, los, max_time,
             max_elevation_deg, aos_azimuth_deg, los_azimuth_deg, duration_s,
             direction, priority, predicted_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sat_pass.satellite, sat_pass.norad_id, sat_pass.freq_mhz,
                sat_pass.decoder, sat_pass.aos.isoformat(), sat_pass.los.isoformat(),
                sat_pass.max_time.isoformat(), sat_pass.max_elevation_deg,
                sat_pass.aos_azimuth_deg, sat_pass.los_azimuth_deg, sat_pass.duration_s,
                sat_pass.direction, sat_pass.priority,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute(
            "SELECT id FROM passes WHERE norad_id=? AND aos=?",
            (sat_pass.norad_id, sat_pass.aos.isoformat()),
        ).fetchone()
        return int(row["id"]) if row else -1

    def record_passes(self, passes: List[Pass]) -> None:
        for p in passes:
            self.record_pass(p)

    def record_capture(self, result: CaptureResult, pass_id: Optional[int] = None) -> int:
        sp = result.sat_pass
        score = compute_pass_score(sp.max_elevation_deg, sp.duration_s, result.snr_db)
        cur = self.conn.execute(
            """
            INSERT INTO captures
            (pass_id, satellite, aos, wav_path, image_count, snr_db,
             recorded_seconds, quality_score, dry_run, ok, error,
             started_at, stopped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                pass_id, sp.satellite, sp.aos.isoformat(),
                str(result.wav_path) if result.wav_path else None,
                len(result.image_paths), result.snr_db, result.recorded_seconds,
                score, int(result.dry_run), int(result.ok), result.error,
                result.started_at.isoformat() if result.started_at else None,
                result.stopped_at.isoformat() if result.stopped_at else None,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_doppler(self, pass_id: int, satellite: str, samples: List[dict]) -> None:
        self.conn.executemany(
            """
            INSERT INTO doppler
            (pass_id, satellite, t, elevation_deg, range_km, range_rate_km_s,
             doppler_hz, corrected_freq_hz)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (
                    pass_id, satellite, s["t"], s["elevation_deg"], s["range_km"],
                    s["range_rate_km_s"], s["doppler_hz"], s["corrected_freq_hz"],
                )
                for s in samples
            ],
        )
        self.conn.commit()

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[dict]:
        return dict(row) if row is not None else None

    @staticmethod
    def _rows(rows: List[sqlite3.Row]) -> List[dict]:
        return [dict(r) for r in rows]

    def recent_captures(self, limit: int = 20) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM captures ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def recent_passes(self, limit: int = 30) -> List[dict]:
        return self._rows(
            self.conn.execute(
                "SELECT * FROM passes ORDER BY aos DESC LIMIT ?", (limit,)
            ).fetchall()
        )

    def doppler_for_pass(self, pass_id: int) -> List[dict]:
        return self._rows(
            self.conn.execute(
                "SELECT * FROM doppler WHERE pass_id=? ORDER BY t", (pass_id,)
            ).fetchall()
        )

    def pass_by_id(self, pass_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM passes WHERE id=?", (pass_id,)).fetchone()
        return self._row(row)

    def capture_by_id(self, capture_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
        return self._row(row)

    def stats(self) -> dict:
        c = self.conn.execute(
            "SELECT COUNT(*) n, AVG(quality_score) q, AVG(snr_db) s FROM captures WHERE ok=1"
        ).fetchone()
        p = self.conn.execute("SELECT COUNT(*) n FROM passes").fetchone()
        return {
            "passes_logged": p["n"],
            "successful_captures": c["n"],
            "avg_quality_score": round(c["q"], 1) if c["q"] is not None else None,
            "avg_snr_db": round(c["s"], 1) if c["s"] is not None else None,
        }
