#!/usr/bin/env python3
"""
Organizes a messy FLAC library into a consistent structure based on tags.

Layout:
  Artist/[Year] Album/01 - Track Title.flac
  _EPs/Artist/[Year] Album [EP]/01 - Track Title.flac
  _Singles/Artist/[Year] Track Title.flac
  _Compilations/[Year] Album/01 - Artist - Track Title.flac
  _Soundtracks/[Year] Album/01 - Track Title.flac

Dry-run by default. Use --apply to move files.
Use --fetch-art to download missing cover art from Cover Art Archive.

Requires: pip install mutagen requests
"""

import os
import re
import sys
import shutil
import argparse
import unicodedata
from pathlib import Path

try:
    from mutagen.flac import FLAC
except ImportError:
    print("Error: mutagen not installed.  Run: pip install mutagen")
    sys.exit(1)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}
VARIOUS_ARTISTS = {"various artists", "various", "va", "v/a", "v.a.", "v.a"}


def sanitize(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name.strip(". ") or "_"


def get_tag(tags: dict, *keys, default="") -> str:
    for key in keys:
        val = tags.get(key) or tags.get(key.upper())
        if val:
            return str(val[0]).strip()
    return default


def parse_track_number(raw: str) -> int:
    try:
        return int(raw.split("/")[0])
    except (ValueError, AttributeError):
        return 0


def detect_release_type(tags: dict, album: str) -> str:
    raw = get_tag(tags, "releasetype", "~releasetype", "musicbrainz_albumtype").lower()

    if "compilation" in raw:
        return "compilation"
    if "soundtrack" in raw or "score" in raw:
        return "soundtrack"
    if "single" in raw:
        return "single"
    if "ep" in raw:
        return "ep"
    if "album" in raw:
        return "album"

    albumartist = get_tag(tags, "albumartist", "album artist").lower()
    if albumartist in VARIOUS_ARTISTS:
        return "compilation"

    album_lower = album.lower()
    if re.search(r"\bep\b|\(ep\)|\[ep\]", album_lower):
        return "ep"
    if re.search(r"\bsoundtrack\b|\bost\b|\boriginal.*score\b", album_lower):
        return "soundtrack"

    return "album"


def build_target_dir(library_root: Path, release_type: str,
                     artist: str, album: str, year: str) -> Path:
    artist = sanitize(artist) if artist else "_Unknown Artist"
    album  = sanitize(album)  if album  else "_Unknown Album"
    prefix = f"[{year}] " if year else ""

    if release_type == "compilation":
        return library_root / "_Compilations" / f"{prefix}{album}"
    if release_type == "soundtrack":
        return library_root / "_Soundtracks" / f"{prefix}{album}"
    if release_type == "ep":
        ep_album = album if re.search(r"\bep\b|\(ep\)|\[ep\]", album, re.I) else f"{album} [EP]"
        return library_root / "_EPs" / artist / f"{prefix}{ep_album}"
    if release_type == "single":
        return library_root / "_Singles" / artist
    # album
    return library_root / artist / f"{prefix}{album}"


def build_filename(release_type: str, track_num: int,
                   track_artist: str, title: str) -> str:
    title        = sanitize(title)        if title        else "_Unknown Title"
    track_artist = sanitize(track_artist) if track_artist else "_Unknown Artist"
    num = f"{track_num:02d} - " if track_num else ""

    if release_type == "compilation":
        return f"{num}{track_artist} - {title}.flac"
    if release_type == "single":
        return f"{title}.flac"
    return f"{num}{title}.flac"


def has_cover(flac_files: list, album_dir: Path) -> bool:
    for name in ("cover", "folder", "front", "artwork"):
        for ext in IMAGE_EXTENSIONS:
            if (album_dir / f"{name}{ext}").exists():
                return True
    if flac_files:
        try:
            if FLAC(flac_files[0]).pictures:
                return True
        except Exception:
            pass
    return False


def fetch_cover_art(mbid: str, album_dir: Path) -> bool:
    try:
        import requests
        r = requests.get(
            f"https://coverartarchive.org/release/{mbid}/front",
            timeout=15, allow_redirects=True,
        )
        if r.status_code == 200:
            ext = ".png" if "png" in r.headers.get("content-type", "") else ".jpg"
            (album_dir / f"cover{ext}").write_bytes(r.content)
            return True
    except Exception:
        pass
    return False


class AlbumPlan:
    def __init__(self, source_dir: Path):
        self.source_dir   = source_dir
        self.target_dir   = None
        self.release_type = "album"
        self.mbid         = ""
        self.moves: list[tuple[Path, Path]] = []
        self.warnings: list[str] = []

    @property
    def has_changes(self) -> bool:
        return any(src != dst for src, dst in self.moves)


def plan_album(source_dir: Path, library_root: Path) -> AlbumPlan:
    plan = AlbumPlan(source_dir)
    flac_files = sorted(source_dir.glob("*.flac"))

    if not flac_files:
        return plan

    # Album-level tags from the first track
    try:
        first = FLAC(flac_files[0])
        tags  = dict(first.tags or {})
    except Exception as e:
        plan.warnings.append(f"Could not read tags: {e}")
        return plan

    albumartist = get_tag(tags, "albumartist", "album artist") or get_tag(tags, "artist")
    album       = get_tag(tags, "album")
    year        = get_tag(tags, "date", "year")[:4]
    plan.mbid   = get_tag(tags, "musicbrainz_albumid")

    if not albumartist:
        plan.warnings.append("Missing albumartist/artist tag")
    if not album:
        plan.warnings.append("Missing album tag")

    plan.release_type = detect_release_type(tags, album)
    plan.target_dir   = build_target_dir(library_root, plan.release_type,
                                         albumartist, album, year)

    # Per-track moves
    for flac in flac_files:
        try:
            a = FLAC(flac)
            t = dict(a.tags or {})
        except Exception:
            plan.warnings.append(f"Could not read tags from {flac.name}")
            continue

        title        = get_tag(t, "title")
        track_artist = get_tag(t, "artist")
        track_num    = parse_track_number(get_tag(t, "tracknumber"))

        if not title:
            plan.warnings.append(f"Missing title: {flac.name}")

        fname = build_filename(plan.release_type, track_num, track_artist, title)
        plan.moves.append((flac, plan.target_dir / fname))

    # Image files travel with the album
    for f in source_dir.iterdir():
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
            plan.moves.append((f, plan.target_dir / f.name))

    return plan


def main():
    parser = argparse.ArgumentParser(
        description="Organize FLAC library into a consistent structure."
    )
    parser.add_argument("library",      help="Path to the music library directory")
    parser.add_argument("--apply",      action="store_true",
                        help="Apply changes (default is dry-run)")
    parser.add_argument("--fetch-art",  action="store_true",
                        help="Download missing cover art from Cover Art Archive")
    args = parser.parse_args()

    library_root = Path(args.library).expanduser().resolve()
    if not library_root.is_dir():
        print(f"Error: '{library_root}' is not a directory.")
        sys.exit(1)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] Library: {library_root}\n")

    # Collect every directory that contains at least one FLAC
    album_dirs: set[Path] = set()
    for root, _, files in os.walk(library_root):
        if any(f.lower().endswith(".flac") for f in files):
            album_dirs.add(Path(root))

    plans = [plan_album(d, library_root) for d in sorted(album_dirs)]

    changes        = [p for p in plans if p.has_changes]
    already_ok     = [p for p in plans if not p.has_changes]
    needs_attention = [p for p in plans if p.warnings]

    print(f"Albums found:       {len(plans)}")
    print(f"Already organized:  {len(already_ok)}")
    print(f"To reorganize:      {len(changes)}")
    print(f"Needs attention:    {len(needs_attention)}")

    if needs_attention:
        print(f"\n{'─' * 60}")
        print("Needs attention (missing tags):")
        for p in needs_attention:
            print(f"  {p.source_dir.relative_to(library_root)}")
            for w in p.warnings:
                print(f"    ! {w}")

    if not changes:
        print("\nNothing to move.")
        return

    print(f"\n{'─' * 60}")
    print("Planned moves:\n")
    for p in changes:
        src_rel = p.source_dir.relative_to(library_root)
        dst_rel = p.target_dir.relative_to(library_root)
        print(f"  [{p.release_type.upper():12s}] {src_rel}")
        print(f"  {'':14s}→ {dst_rel}\n")

    if not args.apply:
        print("Run with --apply to move files.")
        return

    print("Applying...\n")
    moved  = 0
    errors = 0

    for p in changes:
        p.target_dir.mkdir(parents=True, exist_ok=True)

        for src, dst in p.moves:
            if src == dst:
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), dst)
                moved += 1
            except Exception as e:
                print(f"  ERROR {src.name}: {e}")
                errors += 1

        # Cover art
        if args.fetch_art:
            flacs = list(p.target_dir.glob("*.flac"))
            if not has_cover(flacs, p.target_dir) and p.mbid:
                ok = fetch_cover_art(p.mbid, p.target_dir)
                if ok:
                    print(f"  Art downloaded → {p.target_dir.relative_to(library_root)}")

        # Remove empty source directory
        if p.source_dir.exists() and p.source_dir != p.target_dir:
            try:
                if not any(p.source_dir.iterdir()):
                    p.source_dir.rmdir()
            except Exception:
                pass

    print(f"\nMoved:  {moved} file(s)")
    if errors:
        print(f"Errors: {errors}")
        sys.exit(1)


if __name__ == "__main__":
    main()
