#!/usr/bin/env python3
"""
Export Plex music playlists into the MP3 mirror as .m3u8 files.

  - One UTF-8 .m3u8 per playlist, written to the mirror root, so the entries
    are plain relative paths (Artist/[Year] Album/01 - Title.mp3) that resolve
    the same on every device the mirror is pushed to (Rockbox, Neutron)
  - Library paths are mapped to their mirror files (.flac -> .mp3); a track
    with no mirror file yet is skipped and reported (run sync.py first)
  - Smart playlists are exported as a snapshot of their current contents
  - Empty playlists and ones over --max-tracks (e.g. an "all music" smart
    playlist) are skipped
  - Every run replaces the previous export: .m3u8 files at the mirror root
    that carry this tool's marker and no longer match a playlist are removed

Plex is only read, never written. The token comes from $PLEX_TOKEN or from
the server's Preferences.xml (--plex-prefs).
"""

import os
import re
import sys
import json
import argparse
import urllib.request
from pathlib import Path

MARKER = "#EXTENC: UTF-8 exported from Plex by flac_mp3_sync/playlists.py"
DEFAULT_PREFS = ("/opt/docker/appdata/plex/config/Library/Application Support/"
                 "Plex Media Server/Preferences.xml")
FAT_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def playlist_filename(title: str) -> str:
    """Playlist title as a FAT/exFAT-safe file name."""
    name = FAT_UNSAFE.sub("_", title).strip().rstrip(".")
    return (name or "playlist") + ".m3u8"


def mirror_relative(plex_file: str, plex_root: str) -> str | None:
    """Library file as Plex sees it -> path relative to the mirror root, or
    None if it lies outside the library. FLAC becomes MP3; MP3 stays."""
    root = plex_root.rstrip("/") + "/"
    if not plex_file.startswith(root):
        return None
    rel = plex_file[len(root):]
    stem, dot, ext = rel.rpartition(".")
    if dot and ext.lower() == "flac":
        rel = stem + ".mp3"
    return rel


def render_m3u8(entries: list[tuple[int, str, str]]) -> str:
    """entries: (duration seconds, "Artist - Title", relative path)."""
    lines = ["#EXTM3U", MARKER]
    for secs, label, rel in entries:
        lines.append(f"#EXTINF:{secs},{label}")
        lines.append(rel)
    return "\n".join(lines) + "\n"


class Plex:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def get(self, path: str) -> dict:
        sep = "&" if "?" in path else "?"
        req = urllib.request.Request(f"{self.url}{path}{sep}X-Plex-Token={self.token}",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["MediaContainer"]


def read_token(prefs: str) -> str:
    token = os.environ.get("PLEX_TOKEN")
    if token:
        return token
    m = re.search(r'PlexOnlineToken="([^"]+)"', Path(prefs).read_text())
    if not m:
        sys.exit(f"no PlexOnlineToken in {prefs}; set PLEX_TOKEN instead")
    return m.group(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("mirror", type=Path, help="MP3 mirror root (sync.py target)")
    ap.add_argument("--plex-url", default="http://127.0.0.1:32400")
    ap.add_argument("--plex-prefs", default=DEFAULT_PREFS)
    ap.add_argument("--section", default="Music", help="Plex music library name")
    ap.add_argument("--max-tracks", type=int, default=2000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    plex = Plex(args.plex_url, read_token(args.plex_prefs))
    sections = [d for d in plex.get("/library/sections")["Directory"]
                if d["title"] == args.section and d["type"] == "artist"]
    if not sections:
        sys.exit(f"no Plex music library named {args.section!r}")
    roots = [loc["path"] for loc in sections[0]["Location"]]

    written, problems = set(), []
    for pl in plex.get("/playlists?playlistType=audio").get("Metadata", []):
        title, count = pl["title"], int(pl.get("leafCount", 0))
        if count == 0 or count > args.max_tracks:
            print(f"  skip  {title} ({count} tracks)")
            continue
        entries, missing = [], 0
        for t in plex.get(f"/playlists/{pl['ratingKey']}/items").get("Metadata", []):
            parts = [p for m in t.get("Media", []) for p in m.get("Part", [])]
            rel = next((r for p in parts for root in roots
                        if (r := mirror_relative(p["file"], root))), None)
            if rel is None or not (args.mirror / rel).is_file():
                missing += 1
                problems.append(f"{title}: {t.get('grandparentTitle')} - {t.get('title')}")
                continue
            label = f"{t.get('originalTitle') or t.get('grandparentTitle', '')} - {t.get('title', '')}"
            entries.append((int(t.get("duration", 0)) // 1000, label, rel))
        if not entries:
            print(f"  skip  {title} (no tracks in the mirror)")
            continue
        name = playlist_filename(title)
        written.add(name)
        if not args.dry_run:
            (args.mirror / name).write_text(render_m3u8(entries), encoding="utf-8")
        note = f", {missing} not in mirror" if missing else ""
        print(f"  wrote {name} ({len(entries)} tracks{note})")

    for old in args.mirror.glob("*.m3u8"):
        if old.name in written:
            continue
        try:
            ours = MARKER in old.read_text(encoding="utf-8", errors="replace")[:500]
        except OSError:
            ours = False
        if ours:
            print(f"  removed stale {old.name}")
            if not args.dry_run:
                old.unlink()

    if problems:
        print(f"\nWARNING: {len(problems)} track(s) skipped, no mirror file (run sync.py first):")
        for p in problems:
            print(f"  - {p}")


if __name__ == "__main__":
    main()
