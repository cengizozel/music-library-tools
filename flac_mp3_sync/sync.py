#!/usr/bin/env python3
"""
One-way sync: FLAC library → MP3 directory.

  - New FLAC files are converted to MP3
  - Changed FLACs (mtime newer than existing MP3) are reconverted
  - Unchanged FLACs are skipped
  - MP3s with no corresponding FLAC are deleted
  - Non-audio files (cover art, etc.) are copied as-is

The MP3 directory is always a derived copy — never edit it directly.

Requires: ffmpeg  (brew install ffmpeg)
"""

import os
import sys
import shutil
import argparse
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif"}


def mp3_path(flac_file: Path, source_root: Path, target_root: Path) -> Path:
    relative = flac_file.relative_to(source_root)
    return target_root / relative.with_suffix(".mp3")


def needs_conversion(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    return src.stat().st_mtime > dst.stat().st_mtime


def convert(src: Path, dst: Path, bitrate: str) -> tuple[Path, bool, str]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(src),
            "-map_metadata", "0",
            "-id3v2_version", "3",
            "-codec:a", "libmp3lame",
            "-b:a", bitrate,
            "-map", "0:a",        # audio stream only, no embedded cover in MP3
            str(dst),
        ],
        capture_output=True,
        text=True,
    )
    ok = result.returncode == 0
    error = result.stderr.strip() if not ok else ""
    return src, ok, error


def sync_images(source_root: Path, target_root: Path):
    for root, _, files in os.walk(source_root):
        for fname in files:
            if Path(fname).suffix.lower() in IMAGE_EXTENSIONS:
                src = Path(root) / fname
                relative = src.relative_to(source_root)
                dst = target_root / relative
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                    shutil.copy2(src, dst)


def remove_orphans(source_root: Path, target_root: Path) -> list[Path]:
    removed = []
    for root, dirs, files in os.walk(target_root, topdown=False):
        for fname in files:
            mp3 = Path(root) / fname
            ext = mp3.suffix.lower()
            if ext == ".mp3":
                relative = mp3.relative_to(target_root).with_suffix(".flac")
                flac = source_root / relative
                if not flac.exists():
                    mp3.unlink()
                    removed.append(mp3)
            elif ext in IMAGE_EXTENSIONS:
                relative = mp3.relative_to(target_root)
                src = source_root / relative
                if not src.exists():
                    mp3.unlink()
                    removed.append(mp3)
        # remove empty directories
        mp3_dir = Path(root)
        if mp3_dir != target_root and not any(mp3_dir.iterdir()):
            mp3_dir.rmdir()
    return removed


def collect_flacs(source_root: Path) -> list[Path]:
    return [
        Path(root) / fname
        for root, _, files in os.walk(source_root)
        for fname in files
        if fname.lower().endswith(".flac")
    ]


def main():
    parser = argparse.ArgumentParser(description="Sync FLAC library to MP3 directory.")
    parser.add_argument("source", help="Source FLAC library directory")
    parser.add_argument("target", help="Target MP3 directory")
    parser.add_argument("--bitrate", default="320k",
                        help="MP3 bitrate (default: 320k)")
    parser.add_argument("--jobs", type=int, default=4,
                        help="Parallel conversion threads (default: 4)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without making changes")
    args = parser.parse_args()

    if not shutil.which("ffmpeg"):
        print("Error: 'ffmpeg' not found. Install it with: brew install ffmpeg")
        sys.exit(1)

    source_root = Path(args.source).expanduser().resolve()
    target_root = Path(args.target).expanduser().resolve()

    if not source_root.is_dir():
        print(f"Error: source '{source_root}' is not a directory.")
        sys.exit(1)

    if args.dry_run:
        print(f"[DRY RUN] Source: {source_root}")
        print(f"[DRY RUN] Target: {target_root}\n")
    else:
        print(f"Source: {source_root}")
        print(f"Target: {target_root}\n")
        target_root.mkdir(parents=True, exist_ok=True)

    # --- orphan removal ---
    to_remove = []
    if target_root.exists():
        to_remove = remove_orphans(source_root, target_root) if not args.dry_run else [
            Path(root) / fname
            for root, _, files in os.walk(target_root)
            for fname in files
            if fname.lower().endswith(".mp3")
            and not (source_root / (Path(root) / fname).relative_to(target_root).with_suffix(".flac")).exists()
        ]

    if to_remove:
        print(f"Removed {len(to_remove)} orphaned file(s):")
        for p in sorted(to_remove):
            print(f"  - {p.relative_to(target_root)}")
        print()

    # --- image sync ---
    if not args.dry_run:
        sync_images(source_root, target_root)

    # --- conversion ---
    flac_files = collect_flacs(source_root)
    to_convert = [
        f for f in flac_files
        if needs_conversion(f, mp3_path(f, source_root, target_root))
    ]
    to_skip = len(flac_files) - len(to_convert)

    print(f"Total FLACs:  {len(flac_files)}")
    print(f"Skipped:      {to_skip}  (unchanged)")
    print(f"To convert:   {len(to_convert)}\n")

    if args.dry_run:
        for f in sorted(to_convert):
            print(f"  would convert: {f.relative_to(source_root)}")
        return

    if not to_convert:
        print("Nothing to do.")
        return

    errors = []
    done = 0

    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(convert, f, mp3_path(f, source_root, target_root), args.bitrate): f
            for f in to_convert
        }
        for future in as_completed(futures):
            src, ok, error = future.result()
            done += 1
            status = "OK" if ok else "FAIL"
            print(f"  [{done}/{len(to_convert)}] {status}  {src.relative_to(source_root)}")
            if not ok:
                errors.append((src, error))

    print(f"\n{'─' * 60}")
    print(f"Converted: {len(to_convert) - len(errors)}")
    print(f"Failed:    {len(errors)}")

    if errors:
        print("\nFailed files:")
        for src, error in sorted(errors):
            print(f"  {src}")
            if error:
                for line in error.splitlines()[-3:]:
                    print(f"    {line}")
        sys.exit(1)


if __name__ == "__main__":
    main()
