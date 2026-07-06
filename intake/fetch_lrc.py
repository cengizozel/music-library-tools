#!/usr/bin/env python3
"""
Walk the music library and write .lrc sidecar files by querying LRCLib.net
directly from file tags — works whether or not tracks are in the beets DB.

Usage:
    venv/bin/python intake/fetch_lrc.py --config intake.toml
    venv/bin/python intake/fetch_lrc.py --config intake.toml --force     # overwrite existing
    venv/bin/python intake/fetch_lrc.py --config intake.toml --dir path  # specific dir
"""

import argparse
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import json
from pathlib import Path

try:
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3NoHeaderError
except ImportError:
    sys.exit("mutagen not found — run: venv/bin/pip install mutagen")

REPO_ROOT = Path(__file__).resolve().parent.parent
LRCLIB_API = "https://lrclib.net/api/get"
USER_AGENT = "music-library-tools/1.0 (https://github.com/cengizozel/music-library-tools)"


def read_tags(path: Path) -> dict | None:
    """Return {artist, title, album, duration} from file tags, or None."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".flac":
            audio = FLAC(str(path))
            get = lambda k: (audio.get(k) or [""])[0]
            return {
                "artist": get("artist") or get("albumartist"),
                "title": get("title"),
                "album": get("album"),
                "duration": audio.info.length,
            }
        elif suffix == ".mp3":
            audio = MP3(str(path))
            tags = audio.tags
            if tags is None:
                return None
            def id3(k):
                frame = tags.get(k)
                return str(frame) if frame else ""
            return {
                "artist": id3("TPE1") or id3("TPE2"),
                "title": id3("TIT2"),
                "album": id3("TALB"),
                "duration": audio.info.length,
            }
    except Exception:
        return None
    return None


def fetch_lrclib(artist: str, title: str, album: str, duration: float) -> str | None:
    """Return synced LRC text from LRCLib, or None if not found."""
    params = {
        "artist_name": artist,
        "track_name": title,
        "album_name": album,
        "duration": str(int(duration)),
    }
    url = LRCLIB_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("syncedLyrics") or None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Fetch .lrc sidecars from LRCLib")
    parser.add_argument("--config", default=str(REPO_ROOT / "intake.toml"))
    parser.add_argument("--dir", default=None, help="scan only this directory")
    parser.add_argument("--force", action="store_true", help="overwrite existing .lrc files")
    parser.add_argument("--delay", type=float, default=0.3,
                        help="seconds between API requests (default 0.3)")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)

    root = Path(args.dir) if args.dir else Path(cfg["library"]).expanduser()

    files = sorted(
        f for f in root.rglob("*")
        if f.suffix.lower() in (".flac", ".mp3")
        and "_Staging" not in f.parts
    )

    total = len(files)
    print(f"Scanning {total} audio files under {root}")

    written = skipped = no_match = errors = 0
    for i, path in enumerate(files, 1):
        lrc_path = path.with_suffix(".lrc")
        if lrc_path.exists() and not args.force:
            skipped += 1
            continue

        tags = read_tags(path)
        if not tags or not tags["artist"] or not tags["title"]:
            errors += 1
            continue

        lyrics = fetch_lrclib(
            tags["artist"], tags["title"], tags["album"], tags["duration"]
        )
        time.sleep(args.delay)

        if lyrics:
            lrc_path.write_text(lyrics, encoding="utf-8")
            written += 1
            print(f"  [{i}/{total}] {path.parent.name}/{path.name} → .lrc")
        else:
            no_match += 1

        if i % 100 == 0:
            print(f"  progress: {i}/{total} — written {written}, skipped {skipped}, no match {no_match}")

    print(f"\nDone — wrote {written}, skipped {skipped} existing, no match {no_match}"
          + (f", {errors} unreadable" if errors else ""))


if __name__ == "__main__":
    main()
