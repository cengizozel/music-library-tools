#!/usr/bin/env python3
"""
Normalize album cover art for device compatibility (Rockbox / PictureFlow) and
to remove the duplicate-image debris that accumulates when a ripper's own art
(folder.jpg, front.jpeg, WMP thumbnails) lands next to a downloaded cover.jpg.

Per album folder (any dir directly containing audio):
  1. Delete known-junk images: Windows Media Player thumbnails
     (AlbumArtSmall.jpg, AlbumArt_{GUID}_*.jpg) and *.db caches.
  2. De-duplicate by content hash: identical images collapse to ONE, keeping the
     best-named copy (cover > folder > front > largest). Genuinely distinct art
     (back covers, booklet scans) is preserved.
  3. Normalize extensions: .jpeg -> .jpg (Rockbox's art search only looks for
     .jpg/.png/.bmp, so a "cover.jpeg" is invisible on the device).
  4. Ensure the primary image is named cover.<ext> so Rockbox reliably finds it.
  5. Convert progressive JPEGs to baseline with jpegtran. This is LOSSLESS (it
     repackages the existing DCT coefficients; decoded pixels are bit-identical)
     and fixes the "bad album art" PictureFlow reports for progressive JPEGs,
     which Rockbox's decoder cannot read.

Dry-run by default. Use --apply to make changes.

Requires: jpegtran (libjpeg-turbo). Images are never re-encoded, so no quality
is ever lost.

Usage:
  python3 cover_normalizer/normalize.py ~/Music/Library
  python3 cover_normalizer/normalize.py ~/Music/Library --apply
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".wav", ".aiff", ".aif", ".ogg",
                    ".oga", ".opus", ".wma", ".ape", ".wv", ".alac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}

# Windows Media Player / ripper junk that should never be kept.
JUNK_RE = re.compile(r"^(albumartsmall|albumart_\{|folder\.jpg-)", re.IGNORECASE)
JUNK_EXACT = {"thumbs.db", "desktop.ini"}

# Name priority for choosing which copy of a duplicate to keep / what to call
# the primary cover. Lower index = higher priority.
NAME_PRIORITY = ["cover", "front", "folder", "albumartlarge", "album", "art"]


def is_junk(name: str) -> bool:
    low = name.lower()
    return low in JUNK_EXACT or bool(JUNK_RE.match(low))


def name_rank(p: Path) -> int:
    stem = p.stem.lower()
    for i, key in enumerate(NAME_PRIORITY):
        if stem == key or stem.startswith(key):
            return i
    return len(NAME_PRIORITY)


def content_hash(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def is_progressive_jpeg(p: Path) -> bool:
    if p.suffix.lower() not in (".jpg", ".jpeg"):
        return False
    out = subprocess.run(["file", "-b", str(p)], capture_output=True, text=True)
    return "progressive" in out.stdout.lower()


def to_baseline(p: Path) -> bool:
    """Losslessly rewrite a progressive JPEG as baseline. True if it changed."""
    tmp = p.with_suffix(p.suffix + ".baseline")
    r = subprocess.run(["jpegtran", "-copy", "all", "-outfile", str(tmp), str(p)],
                       capture_output=True)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, p)
    return True


def album_dirs(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if any(part.startswith("_") or part == ".music-tools" for part in d.parts):
            continue
        if any(Path(f).suffix.lower() in AUDIO_EXTENSIONS for f in filenames):
            yield d


def unique(target: Path) -> Path:
    if not target.exists():
        return target
    n = 1
    while True:
        cand = target.with_name(f"{target.stem} ({n}){target.suffix}")
        if not cand.exists():
            return cand
        n += 1


class Stats:
    def __init__(self):
        self.junk = self.dups = self.renamed = self.baselined = self.albums = 0


def normalize_album_covers(d: Path, apply: bool = True) -> int:
    """Normalize one album dir's cover art; return the number of changes made.
    Importable entry point used by the intake pipeline (no logging)."""
    st = Stats()
    normalize_album(Path(d), apply, st, lambda _msg: None)
    return st.junk + st.dups + st.renamed + st.baselined


def normalize_album(d: Path, apply: bool, st: Stats, log):
    images = [p for p in d.iterdir()
              if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    if not images:
        return

    # 1. junk
    kept = []
    for p in images:
        if is_junk(p.name):
            log(f"  junk    {p.relative_to(d.parent.parent) if False else p.name}")
            if apply:
                p.unlink()
            st.junk += 1
        else:
            kept.append(p)

    # 2. de-duplicate by content
    by_hash: dict[str, list[Path]] = {}
    for p in kept:
        by_hash.setdefault(content_hash(p), []).append(p)
    survivors = []
    for group in by_hash.values():
        group.sort(key=lambda p: (name_rank(p), -p.stat().st_size, len(p.name)))
        survivors.append(group[0])
        for extra in group[1:]:
            log(f"  dup     {extra.name}  (== {group[0].name})")
            if apply:
                extra.unlink()
            st.dups += 1

    # 3 & 4. extension + primary naming
    survivors.sort(key=name_rank)
    have_cover = any(s.stem.lower() == "cover" for s in survivors)
    renamed_survivors = []
    for i, p in enumerate(survivors):
        new = p
        # .jpeg -> .jpg (Rockbox cannot find .jpeg)
        if p.suffix.lower() == ".jpeg":
            new = p.with_suffix(".jpg")
        # promote the top-priority image to cover.<ext> if no cover exists
        if i == 0 and not have_cover:
            new = new.with_name("cover" + new.suffix.lower())
        if new != p:
            new = unique(new) if new.exists() and new.resolve() != p.resolve() else new
            log(f"  rename  {p.name} -> {new.name}")
            if apply:
                p.rename(new)
            st.renamed += 1
        renamed_survivors.append(new if apply else p)

    # 5. progressive -> baseline (lossless)
    for p in (renamed_survivors if apply else survivors):
        if apply and is_progressive_jpeg(p):
            if to_baseline(p):
                log(f"  baseline {p.name}")
                st.baselined += 1
        elif not apply and is_progressive_jpeg(p):
            log(f"  baseline {p.name}  (progressive -> baseline)")
            st.baselined += 1

    st.albums += 1


def main():
    ap = argparse.ArgumentParser(description="Normalize/dedupe/baseline album cover art.")
    ap.add_argument("library", help="library or mirror root")
    ap.add_argument("--apply", action="store_true", help="make changes (default: dry-run)")
    ap.add_argument("--quiet", action="store_true", help="only print the summary")
    args = ap.parse_args()

    if not shutil.which("jpegtran"):
        print("Error: jpegtran not found (install libjpeg-turbo).")
        sys.exit(1)

    root = Path(args.library).expanduser().resolve()
    if not root.is_dir():
        print(f"Error: '{root}' is not a directory.")
        sys.exit(1)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] {root}\n")
    st = Stats()

    def make_log(header_holder):
        def log(msg):
            if args.quiet:
                return
            if not header_holder[0]:
                print(header_holder[1])
                header_holder[0] = True
            print(msg)
        return log

    for d in album_dirs(root):
        holder = [False, f"{d.relative_to(root)}"]
        normalize_album(d, args.apply, st, make_log(holder))

    print(f"\n{'─' * 60}")
    print(f"Albums scanned:        {st.albums}")
    print(f"Junk thumbnails:       {st.junk}")
    print(f"Duplicate images:      {st.dups}")
    print(f"Renamed (.jpeg/cover): {st.renamed}")
    print(f"Progressive->baseline: {st.baselined}")
    if not args.apply and (st.junk or st.dups or st.renamed or st.baselined):
        print("\nRun with --apply to make these changes.")


if __name__ == "__main__":
    main()
