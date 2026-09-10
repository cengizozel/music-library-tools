#!/usr/bin/env python3
"""
Re-pin proposer: for albums whose files disagree with their pinned MusicBrainz
release (wrong track count or big duration deltas), search the release-group's
sibling releases for the edition the files actually match, and report it.

Read-only. Emits one line per album:
  REPIN <album-path> -> <sibling-mbid> (<why>)
  KEEP  <album-path> (pinned release already best match)
  NOMATCH <album-path> (no sibling matches; likely custom/partial copy)

Usage: propose_repin.py LIB "Artist/Album" [more albums...]  (reads stdin if none)
"""

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from mutagen import File as MutagenFile

MB_UA = "music-library-tools/1.0 ( https://github.com/cengizozel/music-library-tools )"
TOL = 7.0


def mb_get(path, cache, cache_path):
    if path in cache:
        return cache[path]
    req = urllib.request.Request("https://musicbrainz.org/ws/2/" + path,
                                 headers={"User-Agent": MB_UA})
    for attempt in range(6):
        time.sleep(1.5 if attempt == 0 else 9 * attempt)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
            cache[path] = data
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache))
            tmp.replace(cache_path)
            return data
        except Exception as e:
            last = e
    print(f"  fetch failed: {path}: {last}", file=sys.stderr)
    return None


def release_durations(rel):
    out = []
    for m in rel.get("media", []):
        if (m.get("format") or "CD").lower() in ("dvd-video", "dvd", "blu-ray"):
            continue                      # video media never mirrors into files
        for t in m.get("tracks", []):
            out.append((t.get("length") or 0) / 1000)
    return sorted(out)


def score(file_durs, rel_durs):
    if len(file_durs) != len(rel_durs):
        return None
    if not any(rel_durs):
        return None                       # no duration data: cannot score
    deltas = [abs(a - b) for a, b in zip(file_durs, rel_durs) if b > 0]
    bad = sum(1 for d in deltas if d > TOL)
    return (bad, max(deltas) if deltas else 0)


def main():
    lib = Path(sys.argv[1]).resolve()
    albums = sys.argv[2:] or [l.strip() for l in sys.stdin if l.strip()]
    cache_path = lib / ".music-tools" / "mb-repin-cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    for rel_path in albums:
        album = lib / rel_path
        files = sorted(p for p in album.rglob("*")
                       if p.suffix.lower() in (".flac", ".mp3"))
        if not files:
            print(f"NOMATCH {rel_path} (no audio)")
            continue
        tags = MutagenFile(files[0], easy=True)
        mbid = (tags.get("musicbrainz_albumid") or [None])[0] if tags else None
        if not mbid:
            print(f"NOMATCH {rel_path} (no pinned mbid)")
            continue
        file_durs = sorted(MutagenFile(f).info.length for f in files)

        rel = mb_get(f"release/{mbid}?fmt=json&inc=release-groups", cache, cache_path)
        if not rel:
            print(f"NOMATCH {rel_path} (pinned release unfetchable)")
            continue
        rgid = rel.get("release-group", {}).get("id")
        group = mb_get(f"release?release-group={rgid}&fmt=json&inc=recordings&limit=100",
                       cache, cache_path) if rgid else None
        candidates = (group or {}).get("releases", [])

        best = None
        for cand in candidates:
            s = score(file_durs, release_durations(cand))
            if s is None:
                continue
            if best is None or s < best[0]:
                best = (s, cand)
        if best is None:
            print(f"NOMATCH {rel_path} (no sibling with matching track count)")
            continue
        (bad, worst), cand = best
        cid = cand["id"]
        why = (f"{len(file_durs)} trks, {bad} off>{TOL:.0f}s, worst {worst:.0f}s, "
               f"'{cand.get('title','')[:34]}' {cand.get('date','?')} "
               f"{cand.get('country','')} media:{len(cand.get('media',[]))}")
        if cid == mbid:
            print(f"KEEP  {rel_path} ({why})")
        elif bad == 0:
            print(f"REPIN {rel_path} -> {cid} ({why})")
        else:
            print(f"WEAK  {rel_path} -> {cid} ({why})")


if __name__ == "__main__":
    main()
