#!/usr/bin/env python3
"""
Library doctor: re-verify a finished library against its own claims.

Intake validates staging on the way in; nothing re-checked the library after.
This tool audits every album folder for the failure modes that have actually
slipped through over the years:

  1. duplicate audio content INSIDE an album (a broken rip cloning one track
     across a disc, or a double import) - flac -t can never catch this, only
     comparing STREAMINFO MD5s across files can
  2. collision-suffix filenames ("X (1).flac" / "X.1.flac" NEXT TO "X.flac") -
     the trace a silent double import leaves behind
  3. with --mb: the album's files vs the MusicBrainz release it claims to be
     (musicbrainz_albumid tag): track count must agree and every track's
     duration must be within tolerance - catches wrong matches accepted in the
     old strong_rec_thresh=0.40 era, and rips whose content does not match
     their names. MB responses are cached so re-runs are cheap.
  4. with --mirror DIR: album folder set and per-folder audio counts of the
     MP3 mirror must equal the library's

Read-only: reports and exits 1 if anything is flagged, changes nothing.

Usage:
  verify.py LIB                          # checks 1+2 (local, fast)
  verify.py LIB --mb                     # + MusicBrainz re-verification
  verify.py LIB --mirror MP3DIR          # + mirror consistency
  verify.py LIB --mb --album "Artist/[2008] Album"   # one album only
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
except ImportError:
    sys.exit("mutagen required (use the repo venv)")

AUDIO = (".flac", ".mp3")
SKIP_DIRS = {"_Staging", ".music-tools"}
MB_UA = "music-library-tools/1.0 ( https://github.com/cengizozel/music-library-tools )"
COLLISION = re.compile(r"^(?P<base>.+?)(?: \(\d+\)|\.\d)$")


def album_dirs(lib: Path):
    for artist in sorted(lib.iterdir()):
        if not artist.is_dir() or artist.name in SKIP_DIRS:
            continue
        for album in sorted(artist.iterdir()):
            if album.is_dir():
                yield album


def audio_files(album: Path):
    out = []
    for p in sorted(album.rglob("*")):
        if p.is_file() and p.suffix.lower() in AUDIO:
            out.append(p)
    return out


def check_content_dupes(album: Path, files) -> list[str]:
    md5s = {}
    for f in files:
        if f.suffix.lower() != ".flac":
            continue
        try:
            info = FLAC(f).info
        except Exception as e:
            return [f"unreadable FLAC: {f.name} ({e})"]
        sig = info.md5_signature
        if not sig:            # unset by the encoder: no content claim to compare
            continue
        md5s.setdefault(sig, []).append(f.name)
    return [f"identical audio: {' == '.join(names)}"
            for sig, names in md5s.items() if len(names) > 1]


def check_collision_names(files) -> list[str]:
    stems = {f.stem: f for f in files}
    hits = []
    for f in files:
        m = COLLISION.match(f.stem)
        if m and m.group("base") in stems:
            hits.append(f"collision leftover: '{f.name}' next to "
                        f"'{stems[m.group('base')].name}'")
    return hits


def mb_release(mbid: str, cache: dict, cache_path: Path) -> dict | None:
    if mbid in cache:
        return cache[mbid]
    url = f"https://musicbrainz.org/ws/2/release/{mbid}?fmt=json&inc=recordings"
    req = urllib.request.Request(url, headers={"User-Agent": MB_UA})
    for attempt in range(5):
        time.sleep(1.2 if attempt == 0 else 8 * attempt)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
            break
        except Exception as e:
            last = e
    else:
        print(f"    MB fetch failed for {mbid}: {last}", file=sys.stderr)
        return None
    slim = {"title": data.get("title", ""),
            "tracks": [{"disc": m.get("position") or 1,
                        "pos": t.get("position"),
                        "title": t.get("title", ""),
                        "ms": t.get("length")}
                       for m in data.get("media", [])
                       for t in m.get("tracks", [])]}
    cache[mbid] = slim
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(cache_path)
    return slim


def parse_file_position(f: Path, multidisc: bool):
    m = re.match(r"^(\d+)", f.stem)
    if not m:
        return None
    n = m.group(1)
    if multidisc and len(n) >= 3:
        return int(n[:-2]), int(n[-2:])
    return 1, int(n)


def check_against_mb(album: Path, files, tolerance: int,
                     cache: dict, cache_path: Path) -> list[str]:
    tagged = MutagenFile(files[0], easy=True)
    mbid = (tagged.get("musicbrainz_albumid") or [None])[0] if tagged else None
    if not mbid:
        return []                       # asis album: nothing claimed, nothing to check
    rel = mb_release(mbid, cache, cache_path)
    if rel is None:
        return [f"MB release {mbid} unfetchable (retry later)"]
    if len(rel["tracks"]) != len(files):
        return [f"track count: {len(files)} file(s) vs {len(rel['tracks'])} "
                f"on MB release '{rel['title']}' ({mbid})"]
    expected = {(t["disc"], t["pos"]): t for t in rel["tracks"]}
    multidisc = len({t["disc"] for t in rel["tracks"]}) > 1
    # a flat 1..N numbering over a multi-disc release maps sequentially
    if multidisc:
        nums = [parse_file_position(f, False) for f in files]
        flat = [n[1] for n in nums if n]
        if flat and max(flat) == len(rel["tracks"]) and len(set(flat)) == len(flat):
            seq = {}
            i = 0
            for t in sorted(rel["tracks"], key=lambda t: (t["disc"], t["pos"])):
                i += 1
                seq[(1, i)] = t
            expected = seq
            multidisc = False
    problems = []
    matched = 0
    positioned = 0
    for f in files:
        pos = parse_file_position(f, multidisc)
        t = expected.get(pos) if pos else None
        if t is not None:
            positioned += 1
        if t is None or t["ms"] is None:
            continue
        try:
            dur = MutagenFile(f).info.length
        except Exception:
            problems.append(f"unreadable: {f.name}")
            continue
        matched += 1
        delta = abs(dur - t["ms"] / 1000)
        if delta > tolerance:
            problems.append(
                f"duration off {delta:.0f}s: {f.name} is "
                f"{int(dur)//60}:{int(dur)%60:02d}, MB track "
                f"{t['disc']}-{t['pos']} '{t['title'][:40]}' expects "
                f"{t['ms']//60000}:{(t['ms']//1000)%60:02d}")
    if matched == 0:
        if positioned:
            problems.append(f"note: MB release has no track durations "
                            f"({positioned} file(s) positioned) — unverifiable")
        else:
            problems.append("could not map any file to an MB track position")
    return problems


def check_mirror(lib: Path, mirror: Path) -> list[str]:
    def counts(root: Path, exts):
        out = {}
        for album in album_dirs(root):
            n = sum(1 for p in album.rglob("*")
                    if p.is_file() and p.suffix.lower() in exts)
            out[str(album.relative_to(root))] = n
        return out
    src = counts(lib, AUDIO)
    dst = counts(mirror, (".mp3",))
    problems = []
    for k in sorted(set(src) - set(dst)):
        problems.append(f"missing from mirror: {k}")
    for k in sorted(set(dst) - set(src)):
        problems.append(f"orphan in mirror: {k}")
    for k in sorted(set(src) & set(dst)):
        if src[k] != dst[k]:
            problems.append(f"mirror count: {k} library {src[k]} vs mirror {dst[k]}")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("library", type=Path)
    ap.add_argument("--mb", action="store_true",
                    help="re-verify matched albums against their MB release")
    ap.add_argument("--mirror", type=Path, help="MP3 mirror to compare")
    ap.add_argument("--album", help="limit to one 'Artist/Album' relative path")
    ap.add_argument("--tolerance", type=int, default=7,
                    help="seconds of duration slack vs MB (default 7)")
    args = ap.parse_args()

    lib = args.library.resolve()
    cache_path = lib / ".music-tools" / "mb-verify-cache.json"
    cache = {}
    if args.mb and cache_path.exists():
        cache = json.loads(cache_path.read_text())

    flagged = 0
    checked = 0
    for album in album_dirs(lib):
        rel = str(album.relative_to(lib))
        if args.album and rel != args.album:
            continue
        files = audio_files(album)
        if not files:
            print(f"! {rel}\n    no audio files")
            flagged += 1
            continue
        checked += 1
        problems = check_content_dupes(album, files)
        problems += check_collision_names(files)
        if args.mb:
            problems += check_against_mb(album, files, args.tolerance,
                                         cache, cache_path)
        if problems:
            flagged += 1
            print(f"! {rel}")
            for p in problems:
                print(f"    {p}")

    if args.mirror:
        for p in check_mirror(lib, args.mirror.resolve()):
            flagged += 1
            print(f"! {p}")

    print(f"\nchecked {checked} album(s); {flagged} flagged")
    sys.exit(1 if flagged else 0)


if __name__ == "__main__":
    main()
