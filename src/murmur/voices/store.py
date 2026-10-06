"""Persistent voice library: match speaker embeddings to known or new voices.

Each voice has a running-mean centroid (unit-norm speaker embedding). A new
embedding is compared to every centroid by cosine similarity and falls into one
of three zones:

* ``>= match`` (and clearly ahead of the runner-up) → that voice;
* ``< new`` and long enough → a brand-new voice ("voice-7");
* in between → undecided: no label, so as not to guess or grow junk voices.

Voices start anonymous and gain names three ways: explicit enrolment/naming via
the CLI, the self-bootstrap (the dominant voice when no call or media is
happening is the user), and calendar hints written by the nightly processing
task (the only other voice in a 1:1 meeting is that attendee).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

_log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    name_source TEXT,
    centroid BLOB NOT NULL,
    n INTEGER NOT NULL DEFAULT 1,
    talk_s REAL NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voice_id INTEGER,
    ts REAL NOT NULL,
    dur REAL NOT NULL,
    sim REAL NOT NULL,
    context TEXT
);
CREATE INDEX IF NOT EXISTS obs_ts ON observations(ts);
CREATE INDEX IF NOT EXISTS obs_voice ON observations(voice_id);
CREATE TABLE IF NOT EXISTS hints_applied (
    filename TEXT PRIMARY KEY,
    mtime REAL NOT NULL
);
"""


@dataclass(frozen=True)
class Thresholds:
    match: float = 0.50  # assign to an existing voice
    new: float = 0.40  # below this, start a new voice
    update: float = 0.60  # confident enough to refine that voice's centroid
    margin: float = 0.05  # best must beat the runner-up by this (unless >= update)
    min_new_s: float = 2.0  # minimum speech to start a new voice


@dataclass(frozen=True)
class Identity:
    voice_id: int | None
    name: str | None
    sim: float

    @property
    def label(self) -> str | None:
        if self.voice_id is None:
            return None
        return self.name or f"voice-{self.voice_id}"


@dataclass(frozen=True)
class VoiceInfo:
    id: int
    name: str | None
    name_source: str | None
    n: int
    talk_s: float
    first_seen: float
    last_seen: float

    @property
    def label(self) -> str:
        return self.name or f"voice-{self.id}"


def _blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def _vec(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class VoiceStore:
    def __init__(self, path: Path, thresholds: Thresholds | None = None) -> None:
        self.path = path
        self.t = thresholds or Thresholds()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        os.chmod(path, 0o600)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ matching
    def identify(
        self, emb: np.ndarray, *, ts: float, dur: float, context: str | None = None
    ) -> Identity:
        e = _unit(np.asarray(emb, dtype=np.float32))
        with self._lock:
            rows = self._db.execute("SELECT id, name, centroid, n FROM voices").fetchall()
            ident = self._decide(e, rows, dur)
            if ident.voice_id is None and ident.sim < self.t.new and dur >= self.t.min_new_s:
                cur = self._db.execute(
                    "INSERT INTO voices(centroid, n, talk_s, first_seen, last_seen) VALUES (?,1,0,?,?)",
                    (_blob(e), ts, ts),
                )
                ident = Identity(int(cur.lastrowid or 0), None, 1.0)
            elif ident.voice_id is not None and ident.sim >= self.t.update:
                row = next(r for r in rows if r[0] == ident.voice_id)
                weight = min(int(row[3]), 50)  # keep adapting to drift
                c = _unit(_vec(row[2]) * weight + e)
                self._db.execute(
                    "UPDATE voices SET centroid=?, n=n+1 WHERE id=?", (_blob(c), ident.voice_id)
                )
            if ident.voice_id is not None:
                self._db.execute(
                    "UPDATE voices SET talk_s=talk_s+?, last_seen=MAX(last_seen, ?) WHERE id=?",
                    (dur, ts, ident.voice_id),
                )
            self._db.execute(
                "INSERT INTO observations(voice_id, ts, dur, sim, context) VALUES (?,?,?,?,?)",
                (ident.voice_id, ts, dur, ident.sim, context),
            )
            self._db.commit()
            return ident

    def _decide(self, e: np.ndarray, rows: list[Any], dur: float) -> Identity:
        if not rows:
            return Identity(None, None, -1.0)
        sims = np.array([float(_vec(r[2]) @ e) for r in rows])
        order = np.argsort(-sims)
        best = float(sims[order[0]])
        second = float(sims[order[1]]) if len(order) > 1 else -1.0
        if best >= self.t.match and (best - second >= self.t.margin or best >= self.t.update):
            r = rows[int(order[0])]
            return Identity(int(r[0]), r[1], best)
        return Identity(None, None, best)

    # ------------------------------------------------------------------ naming
    def voices(self) -> list[VoiceInfo]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, name, name_source, n, talk_s, first_seen, last_seen "
                "FROM voices ORDER BY talk_s DESC"
            ).fetchall()
        return [VoiceInfo(*r) for r in rows]

    def resolve(self, ref: str | int) -> int | None:
        """Voice id from an id, "voice-7", or a (case-insensitive) name."""
        if isinstance(ref, int):
            return ref
        s = str(ref).strip()
        if s.lower().startswith("voice-") and s[6:].isdigit():
            return int(s[6:])
        if s.isdigit():
            return int(s)
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM voices WHERE lower(name)=lower(?) ORDER BY talk_s DESC", (s,)
            ).fetchone()
        return int(row[0]) if row else None

    def name(self, ref: str | int, name: str, source: str = "user") -> int:
        """Name a voice. If another voice already has that name, merge into it."""
        vid = self.resolve(ref)
        if vid is None:
            raise KeyError(f"No such voice: {ref}")
        existing = self.resolve(name)
        if existing is not None and existing != vid:
            self.merge(vid, existing)
            return existing
        with self._lock:
            self._db.execute(
                "UPDATE voices SET name=?, name_source=? WHERE id=?", (name.strip(), source, vid)
            )
            self._db.commit()
        return vid

    def merge(self, src: str | int, dst: str | int) -> int:
        s, d = self.resolve(src), self.resolve(dst)
        if s is None or d is None or s == d:
            raise KeyError(f"Cannot merge {src} into {dst}")
        with self._lock:
            a = self._db.execute("SELECT centroid, n, talk_s, first_seen FROM voices WHERE id=?", (s,)).fetchone()
            b = self._db.execute("SELECT centroid, n, talk_s, first_seen FROM voices WHERE id=?", (d,)).fetchone()
            if not a or not b:
                raise KeyError(f"Cannot merge {src} into {dst}")
            c = _unit(_vec(a[0]) * a[1] + _vec(b[0]) * b[1])
            self._db.execute(
                "UPDATE voices SET centroid=?, n=?, talk_s=?, first_seen=? WHERE id=?",
                (_blob(c), a[1] + b[1], a[2] + b[2], min(a[3], b[3]), d),
            )
            self._db.execute("UPDATE observations SET voice_id=? WHERE voice_id=?", (d, s))
            self._db.execute("DELETE FROM voices WHERE id=?", (s,))
            self._db.commit()
        return d

    def forget(self, ref: str | int) -> bool:
        """Erase a voice and every observation of it."""
        vid = self.resolve(ref)
        if vid is None:
            return False
        with self._lock:
            self._db.execute("DELETE FROM observations WHERE voice_id=?", (vid,))
            cur = self._db.execute("DELETE FROM voices WHERE id=?", (vid,))
            self._db.commit()
            self._db.execute("VACUUM")
            return cur.rowcount > 0

    def enroll(self, embeddings: list[np.ndarray], name: str, source: str = "user") -> int:
        """Create or reinforce a named voice from clean samples."""
        c = _unit(np.mean([_unit(np.asarray(e, np.float32)) for e in embeddings], axis=0))
        now = time.time()
        vid = self.resolve(name)
        with self._lock:
            if vid is None:
                cur = self._db.execute(
                    "INSERT INTO voices(name, name_source, centroid, n, talk_s, first_seen, last_seen) "
                    "VALUES (?,?,?,?,0,?,?)",
                    (name, source, _blob(c), len(embeddings), now, now),
                )
                vid = int(cur.lastrowid or 0)
            else:
                row = self._db.execute("SELECT centroid, n FROM voices WHERE id=?", (vid,)).fetchone()
                merged = _unit(_vec(row[0]) * row[1] + c * len(embeddings))
                self._db.execute(
                    "UPDATE voices SET centroid=?, n=n+?, name_source=? WHERE id=?",
                    (_blob(merged), len(embeddings), source, vid),
                )
            self._db.commit()
        return vid

    # ------------------------------------------------------------------ learning
    def self_voice(self, self_name: str) -> int | None:
        return self.resolve(self_name)

    def bootstrap_self(
        self, self_name: str, *, min_talk_s: float = 600.0, dominance: float = 2.0
    ) -> int | None:
        """Name the user's voice if not yet known.

        When no call is active and no media is playing, the person talking is
        almost always the user — so the voice with by far the most talk time in
        those moments is them.
        """
        if self.self_voice(self_name) is not None:
            return None
        with self._lock:
            rows = self._db.execute(
                "SELECT o.voice_id, SUM(o.dur) AS t FROM observations o JOIN voices v ON v.id=o.voice_id "
                "WHERE o.context IS NULL AND v.name IS NULL GROUP BY o.voice_id ORDER BY t DESC LIMIT 2"
            ).fetchall()
        if not rows or rows[0][1] < min_talk_s:
            return None
        if len(rows) > 1 and rows[0][1] < dominance * rows[1][1]:
            return None
        vid = int(rows[0][0])
        self.name(vid, self_name, source="self-auto")
        _log.info("Learned the user's voice: voice-%d → %s", vid, self_name)
        return vid

    def apply_one_on_one(
        self, start_ts: float, end_ts: float, person: str, self_name: str, *, min_talk_s: float = 20.0
    ) -> int | None:
        """In a 1:1 meeting, the dominant non-user voice is the other attendee."""
        me = self.self_voice(self_name)
        with self._lock:
            rows = self._db.execute(
                "SELECT voice_id, SUM(dur) AS t FROM observations WHERE ts BETWEEN ? AND ? "
                "AND voice_id IS NOT NULL AND voice_id IS NOT ? GROUP BY voice_id ORDER BY t DESC LIMIT 2",
                (start_ts, end_ts, me),
            ).fetchall()
        if not rows or rows[0][1] < min_talk_s:
            return None
        if len(rows) > 1 and rows[0][1] < 2.0 * rows[1][1]:
            return None  # ambiguous: two strong voices besides the user
        vid = int(rows[0][0])
        current = next((v for v in self.voices() if v.id == vid), None)
        if current is not None and current.name and current.name.lower() != person.lower():
            _log.info("Hint says %s but voice-%d is already %s; leaving it", person, vid, current.name)
            return None
        return self.name(vid, person, source="calendar")

    def apply_hint_files(self, hints_dir: Path, self_name: str) -> int:
        """Apply new/changed ``YYYY-MM-DD.json`` hint files. Returns voices named."""
        if not hints_dir.is_dir():
            return 0
        named = 0
        for f in sorted(hints_dir.glob("*.json")):
            mtime = f.stat().st_mtime
            with self._lock:
                row = self._db.execute(
                    "SELECT mtime FROM hints_applied WHERE filename=?", (f.name,)
                ).fetchone()
            if row and row[0] >= mtime:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                day = datetime.strptime(f.stem, "%Y-%m-%d").date()
            except (OSError, ValueError) as exc:
                _log.warning("Ignoring speaker hint file %s: %s", f.name, exc)
                continue
            for h in data.get("hints", []):
                if h.get("kind") != "one_on_one" or not h.get("person"):
                    continue
                try:
                    start = datetime.combine(day, datetime.strptime(h["start"], "%H:%M").time())
                    end = datetime.combine(day, datetime.strptime(h["end"], "%H:%M").time())
                except (KeyError, ValueError):
                    continue
                pad = 300.0
                if self.apply_one_on_one(
                    start.astimezone().timestamp() - pad,
                    end.astimezone().timestamp() + pad,
                    str(h["person"]),
                    self_name,
                ) is not None:
                    named += 1
            with self._lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO hints_applied(filename, mtime) VALUES (?,?)", (f.name, mtime)
                )
                self._db.commit()
        return named

    def prune(self, max_age_days: float = 90.0) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM observations WHERE ts < ?", (time.time() - max_age_days * 86400,)
            )
            self._db.commit()

    # ------------------------------------------------------------------ export
    def export_json(self, path: Path) -> None:
        """Write names/ids (no embeddings) for the nightly processing task."""
        data = {
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "voices": [
                {
                    "label": v.label,
                    "name": v.name,
                    "named_by": v.name_source,
                    "talk_minutes": round(v.talk_s / 60, 1),
                    "first_seen": datetime.fromtimestamp(v.first_seen).astimezone().isoformat(timespec="minutes"),
                    "last_seen": datetime.fromtimestamp(v.last_seen).astimezone().isoformat(timespec="minutes"),
                }
                for v in self.voices()
            ],
        }
        tmp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            _log.warning("Could not export voices to %s: %s", path, exc)


def wipe(path: Path) -> bool:
    """Delete the whole voice library."""
    removed = False
    for p in (path, path.with_name(path.name + "-journal"), path.with_name(path.name + "-wal")):
        if p.exists():
            p.unlink()
            removed = True
    return removed
