#!/usr/bin/env python3
"""
Persistent memory for manual intake decisions, keyed by audio content (see audio_keys).

The store lives inside the library (<library>/.music-tools/decisions.json) so it is
backed up together with the music. It is saved after every mutation (atomic
write-to-temp + rename), so an interrupted run never loses answers.

Sections:
  tracks      track_key -> {"allowed_mp3": bool, "corrupt": "ignore"|"seen",
                            "format": "keep"|"converted"|..., "note": str}
  albums      album_key -> {"action": "mbid"|"asis", "mbid": str,
                            "artist": str, "album": str, "year": str,
                            "release_type": str, "track_keys": [...], "decided_at": iso}
  artist_map  lowercased raw albumartist -> canonical primary artist
              (identity mapping means "user said keep as-is")
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from artist_normalizer import fold

SCHEMA_VERSION = 1


class DecisionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.mutations = 0   # decisions saved during this process's lifetime
        self.data = {
            "schema_version": SCHEMA_VERSION,
            "tracks": {},
            "albums": {},
            "artist_map": {},
        }
        if self.path.exists():
            loaded = json.loads(self.path.read_text())
            if loaded.get("schema_version", 1) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path} was written by a newer tool "
                    f"(schema {loaded.get('schema_version')} > {SCHEMA_VERSION})"
                )
            for section in ("tracks", "albums", "artist_map"):
                self.data[section].update(loaded.get(section, {}))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # --- tracks ---

    def track(self, key: str) -> dict:
        return self.data["tracks"].get(key, {})

    def set_track(self, key: str, **fields):
        entry = self.data["tracks"].setdefault(key, {})
        entry.update(fields)
        entry["decided_at"] = _now()
        self.mutations += 1
        self.save()

    # --- albums ---

    def album(self, key: str) -> dict:
        return self.data["albums"].get(key, {})

    def set_album(self, key: str, *, action: str, track_keys: list[str], **fields):
        entry = {"action": action, "track_keys": sorted(track_keys),
                 "decided_at": _now(), **fields}
        self.data["albums"][key] = entry
        self.mutations += 1
        self.save()

    def fuzzy_album(self, track_keys: list[str], threshold: float = 0.8) -> tuple[str, dict] | None:
        """Best stored album sharing >= threshold of the given track keys.

        Catches re-staged albums that gained/lost a track (different exact key)."""
        if not track_keys:
            return None
        keys = set(track_keys)
        best = None
        best_overlap = 0.0
        for akey, entry in self.data["albums"].items():
            stored = set(entry.get("track_keys", []))
            if not stored:
                continue
            overlap = len(keys & stored) / max(len(keys), len(stored))
            if overlap > best_overlap:
                best_overlap = overlap
                best = (akey, entry)
        if best and best_overlap >= threshold:
            return best
        return None

    # --- artist map ---

    def canonical_artist(self, raw: str) -> str | None:
        hit = self.data["artist_map"].get(fold(raw))
        if hit is None:
            # entries written before fold() existed used plain lowercasing
            hit = self.data["artist_map"].get(raw.strip().lower())
        return hit

    def set_canonical_artist(self, raw: str, canonical: str):
        self.data["artist_map"][fold(raw)] = canonical.strip()
        self.mutations += 1
        self.save()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
