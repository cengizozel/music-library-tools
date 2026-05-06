#!/usr/bin/env python3
"""
Organizes a music library (FLAC and MP3) into a Plex/Navidrome/Rockbox-compatible structure.

Layout (always):
  Artist/[Year] Project/01 - Track Title.ext
  Various Artists/[Year] Project/01 - Track Title.ext  (compilations)

Multi-disc:
  Artist/[Year] Album/101 - Track Title.ext  (disc 1)
                       201 - Track Title.ext  (disc 2)

Release type (album, EP, single, compilation, soundtrack) is stored in tags only.
Folder names never encode release type except optionally [EP] as a cosmetic suffix.

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
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3NoHeaderError
except ImportError:
    print("Error: mutagen not installed.  Run: pip install mutagen")
    sys.exit(1)

AUDIO_EXTENSIONS  = {".flac", ".mp3"}
IMAGE_EXTENSIONS  = {".jpg", ".jpeg", ".png", ".bmp"}
VARIOUS_ARTISTS   = {"various artists", "various", "va", "v/a", "v.a.", "v.a"}
DISC_FOLDER_RE    = re.compile(r'^(cd|disc|disk)\s*(\d+)$', re.IGNORECASE)


def disc_number_from_name(name: str) -> int:
    m = DISC_FOLDER_RE.match(name.strip())
    return int(m.group(2)) if m else 0


def strip_disc_suffix(album: str) -> str:
    album = re.sub(r'\s*[\(\[](cd|disc|disk)\s*\d+[\)\]]', '', album, flags=re.IGNORECASE)
    album = re.sub(r'\s*(cd|disc|disk)\s*\d+$', '', album, flags=re.IGNORECASE)
    return album.strip()


def get_disc_subfolders(directory: Path) -> list[Path]:
    return sorted(
        (d for d in directory.iterdir() if d.is_dir() and DISC_FOLDER_RE.match(d.name)),
        key=lambda d: disc_number_from_name(d.name),
    )


def sanitize(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name.strip(". ") or "_"


def read_tags(path: Path) -> dict:
    try:
        if path.suffix.lower() == ".flac":
            a = FLAC(path)
            return dict(a.tags or {})
        else:
            a = EasyID3(path)
            return dict(a)
    except (ID3NoHeaderError, Exception):
        return {}


def get_tag(tags: dict, *keys, default="") -> str:
    for key in keys:
        val = tags.get(key) or tags.get(key.upper())
        if val:
            return str(val[0]).strip()
    return default


def parse_number(raw: str) -> int:
    try:
        return int(str(raw).split("/")[0])
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
    # Compilations and soundtracks with various artists go under "Various Artists"
    if release_type in ("compilation", "soundtrack"):
        folder_artist = "Various Artists"
    else:
        folder_artist = sanitize(artist) if artist else "_Unknown Artist"

    album  = sanitize(album) if album else "_Unknown Album"
    prefix = f"[{year}] " if year else ""

    # Optionally keep [EP] as a cosmetic suffix in the folder name
    if release_type == "ep" and not re.search(r"\bep\b|\(ep\)|\[ep\]", album, re.I):
        album = f"{album} [EP]"

    return library_root / folder_artist / f"{prefix}{album}"


def build_filename(track_num: int, disc_num: int, is_multidisc: bool,
                   title: str, ext: str) -> str:
    title = sanitize(title) if title else "_Unknown Title"

    if track_num:
        if is_multidisc and disc_num:
            num = f"{disc_num}{track_num:02d} - "
        else:
            num = f"{track_num:02d} - "
    else:
        num = ""

    return f"{num}{title}{ext}"


def has_cover(audio_files: list, album_dir: Path) -> bool:
    for name in ("cover", "folder", "front", "artwork"):
        for ext in IMAGE_EXTENSIONS:
            if (album_dir / f"{name}{ext}").exists():
                return True
    for f in audio_files:
        try:
            if f.suffix.lower() == ".flac":
                if FLAC(f).pictures:
                    return True
            else:
                from mutagen.id3 import ID3
                if ID3(f).getall("APIC"):
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

    disc_subs = get_disc_subfolders(source_dir)

    if disc_subs:
        # Build (disc_num, path) list from subfolders
        disc_audio: list[tuple[int, Path]] = []
        for disc_dir in disc_subs:
            dn = disc_number_from_name(disc_dir.name)
            for f in sorted(disc_dir.iterdir()):
                if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                    disc_audio.append((dn, f))
        if not disc_audio:
            return plan
        first_file = disc_audio[0][1]
        is_multidisc = len(disc_subs) > 1
    else:
        audio_files = sorted(
            f for f in source_dir.iterdir()
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
        )
        if not audio_files:
            return plan
        # Build (disc_num, path) list from tags
        disc_audio = []
        for f in audio_files:
            t  = read_tags(f)
            dn = parse_number(get_tag(t, "discnumber", "disc")) or 1
            disc_audio.append((dn, f))
        first_file   = audio_files[0]
        disc_numbers = {dn for dn, _ in disc_audio}
        is_multidisc = max(disc_numbers, default=1) > 1

    # Album-level tags from first track (prefer FLAC over MP3)
    flac_first = next((f for _, f in disc_audio if f.suffix.lower() == ".flac"), None)
    tags = read_tags(flac_first or first_file)

    if not tags:
        plan.warnings.append(f"Could not read tags from {first_file.name}")
        return plan

    albumartist = get_tag(tags, "albumartist", "album artist") or get_tag(tags, "artist")
    album       = get_tag(tags, "album")
    if disc_subs:
        album = strip_disc_suffix(album)
    year        = get_tag(tags, "originalyear", "originaldate", "date", "year")[:4]
    plan.mbid   = get_tag(tags, "musicbrainz_albumid")

    if not albumartist:
        plan.warnings.append("Missing albumartist/artist tag")
    if not album:
        plan.warnings.append("Missing album tag")

    plan.release_type = detect_release_type(tags, album)
    plan.target_dir   = build_target_dir(library_root, plan.release_type,
                                         albumartist, album, year)

    # Per-track moves
    for disc_num, audio in disc_audio:
        t   = read_tags(audio)
        ext = audio.suffix.lower()

        title     = get_tag(t, "title")
        track_num = parse_number(get_tag(t, "tracknumber"))

        if not title:
            plan.warnings.append(f"Missing title: {audio.name}")

        fname = build_filename(track_num, disc_num, is_multidisc, title, ext)
        plan.moves.append((audio, plan.target_dir / fname))

    # Image files from source dir travel with the album
    for f in source_dir.iterdir():
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
            plan.moves.append((f, plan.target_dir / f.name))

    return plan


def main():
    parser = argparse.ArgumentParser(
        description="Organize music library into a Plex/Navidrome-compatible structure."
    )
    parser.add_argument("library",     help="Path to the music library directory")
    parser.add_argument("--apply",     action="store_true",
                        help="Apply changes (default is dry-run)")
    parser.add_argument("--fetch-art", action="store_true",
                        help="Download missing cover art from Cover Art Archive")
    args = parser.parse_args()

    library_root = Path(args.library).expanduser().resolve()
    if not library_root.is_dir():
        print(f"Error: '{library_root}' is not a directory.")
        sys.exit(1)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] Library: {library_root}\n")

    album_dirs: set[Path] = set()
    for root, _, files in os.walk(library_root):
        p = Path(root)
        if any(Path(f).suffix.lower() in AUDIO_EXTENSIONS for f in files):
            # If this dir is a disc subfolder, register the parent as the album
            if DISC_FOLDER_RE.match(p.name):
                album_dirs.add(p.parent)
            else:
                album_dirs.add(p)

    plans = [plan_album(d, library_root) for d in sorted(album_dirs)]

    changes         = [p for p in plans if p.has_changes]
    already_ok      = [p for p in plans if not p.has_changes]
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

        if args.fetch_art:
            audio = list(p.target_dir.glob("*.flac")) + list(p.target_dir.glob("*.mp3"))
            if not has_cover(audio, p.target_dir) and p.mbid:
                ok = fetch_cover_art(p.mbid, p.target_dir)
                if ok:
                    print(f"  Art downloaded → {p.target_dir.relative_to(library_root)}")

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
