#!/usr/bin/env python3
"""
One-way sync: FLAC library → MP3 directory.

  - FLAC files are converted to MP3
  - Source MP3 files are copied as-is (no conversion)
  - Both follow the same sync rules:
      new → convert/copy
      changed (mtime newer) → reconvert/recopy
      unchanged → skip
      deleted from source → remove from target
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
LYRIC_EXTENSIONS = {".lrc"}
DEFAULT_EXCLUDES = ["_Staging", ".music-tools"]


def target_path(src: Path, source_root: Path, target_root: Path) -> Path:
    relative = src.relative_to(source_root)
    return target_root / relative.with_suffix(".mp3")


def _exists_ci(path: Path) -> bool:
    """Exists, matching the file name case-insensitively (Song.FLAC vs .flac)."""
    if path.exists():
        return True
    parent = path.parent
    if not parent.is_dir():
        return False
    name = path.name.lower()
    return any(c.name.lower() == name for c in parent.iterdir())


def needs_update(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    return src.stat().st_mtime > dst.stat().st_mtime


def convert(src: Path, dst: Path, bitrate: str) -> tuple[Path, bool, str]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    # write to a temp file so a failed conversion never truncates a good target
    tmp = dst.with_suffix(dst.suffix + ".part.mp3")
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(src),
            "-map_metadata", "0",
            "-id3v2_version", "3",
            "-codec:a", "libmp3lame",
            "-b:a", bitrate,
            "-map", "0:a",
            "-f", "mp3",
            str(tmp),
        ],
        capture_output=True,
        text=True,
    )
    ok = result.returncode == 0
    if ok:
        os.replace(tmp, dst)
        error = ""
    else:
        tmp.unlink(missing_ok=True)
        error = result.stderr.strip()
    return src, ok, error


def copy_file(src: Path, dst: Path) -> tuple[Path, bool, str]:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return src, True, ""
    except Exception as e:
        return src, False, str(e)


# Rockbox/PictureFlow on old players can't decode large or progressive images
# (big PNGs in particular). Cover art copied to the mirror is therefore capped
# to a device-safe baseline JPEG. Audio is untouched; the FLAC library keeps the
# full-resolution original — only the derived mirror copy is downscaled.
DEVICE_COVER_MAX = 500


def _cover_to_device_jpeg(src: Path, dst: Path) -> bool:
    """Write a <=DEVICE_COVER_MAX px baseline JPEG of src at dst (named .jpg).
    True on success. Requires ffmpeg."""
    dst = dst.with_suffix(".jpg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name("_cov_tmp.jpg")
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src),
         "-vf", f"scale='min({DEVICE_COVER_MAX},iw)':'min({DEVICE_COVER_MAX},ih)'"
                ":force_original_aspect_ratio=decrease",
         "-q:v", "3", str(tmp)],
        capture_output=True)
    if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        os.replace(tmp, dst)
        return True
    tmp.unlink(missing_ok=True)
    return False


def sync_images(source_root: Path, target_root: Path, excludes: list[str]):
    have_ffmpeg = shutil.which("ffmpeg") is not None
    for root, dirs, files in os.walk(source_root):
        dirs[:] = [d for d in dirs if d not in excludes]
        for fname in files:
            ext = Path(fname).suffix.lower()
            if ext not in IMAGE_EXTENSIONS | LYRIC_EXTENSIONS:
                continue
            src = Path(root) / fname
            dst = target_root / src.relative_to(source_root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            # cover art -> device-safe capped baseline JPEG
            is_cover = have_ffmpeg and ext in IMAGE_EXTENSIONS and \
                src.stem.lower() in ("cover", "folder", "front")
            if is_cover:
                jpg_dst = dst.with_suffix(".jpg")
                if needs_update(src, jpg_dst):
                    if _cover_to_device_jpeg(src, dst):
                        # a source cover.png becomes cover.jpg on the device;
                        # drop a stale same-name .png so it isn't an orphan
                        if ext == ".png":
                            dst.unlink(missing_ok=True)
                        continue
                else:
                    continue
            if needs_update(src, dst):
                shutil.copy2(src, dst)


def remove_orphans(source_root: Path, target_root: Path, excludes: list[str]) -> list[Path]:
    removed = []
    for root, dirs, files in os.walk(target_root, topdown=False):
        for fname in files:
            tgt = Path(root) / fname
            ext = tgt.suffix.lower()
            relative = tgt.relative_to(target_root)
            # excluded names are out of scope entirely: never sync, never delete
            if any(part in excludes for part in relative.parts):
                continue
            if ext == ".mp3":
                # valid if source has a .flac OR a .mp3 at the same relative
                # path — extension case-insensitively (Song.FLAC mirrors too)
                has_flac = _exists_ci(source_root / relative.with_suffix(".flac"))
                has_mp3  = _exists_ci(source_root / relative)
                if not has_flac and not has_mp3:
                    tgt.unlink()
                    removed.append(tgt)
            elif ext in IMAGE_EXTENSIONS | LYRIC_EXTENSIONS:
                if (source_root / relative).exists():
                    continue
                # a device cover.jpg may derive from a library cover in any
                # image format (cover.png -> cover.jpg); not an orphan then
                if tgt.stem.lower() in ("cover", "folder", "front") and ext == ".jpg":
                    src_dir = source_root / relative.parent
                    if src_dir.is_dir() and any(
                            p.stem.lower() == tgt.stem.lower()
                            and p.suffix.lower() in IMAGE_EXTENSIONS
                            for p in src_dir.iterdir()):
                        continue
                tgt.unlink()
                removed.append(tgt)
        mp3_dir = Path(root)
        if mp3_dir != target_root and not any(mp3_dir.iterdir()):
            mp3_dir.rmdir()
    return removed


def collect(source_root: Path, excludes: list[str]) -> tuple[list[Path], list[Path]]:
    flacs, mp3s = [], []
    for root, dirs, files in os.walk(source_root):
        dirs[:] = [d for d in dirs if d not in excludes]
        for fname in files:
            p = Path(root) / fname
            ext = p.suffix.lower()
            if ext == ".flac":
                flacs.append(p)
            elif ext == ".mp3":
                mp3s.append(p)
    return flacs, mp3s


def main():
    parser = argparse.ArgumentParser(description="Sync FLAC library to MP3 directory.")
    parser.add_argument("source",  help="Source library directory")
    parser.add_argument("target",  help="Target MP3 directory")
    parser.add_argument("--bitrate", default="320k",
                        help="MP3 bitrate for FLAC conversion (default: 320k)")
    parser.add_argument("--jobs", type=int, default=4,
                        help="Parallel threads (default: 4)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without making changes")
    parser.add_argument("--exclude", action="append", default=[], metavar="NAME",
                        help="additional dir names to skip "
                             "(_Staging and .music-tools are always skipped)")
    args = parser.parse_args()
    excludes = DEFAULT_EXCLUDES + args.exclude

    if not shutil.which("ffmpeg"):
        print("Error: 'ffmpeg' not found. Install it with: brew install ffmpeg")
        sys.exit(1)

    source_root = Path(args.source).expanduser().resolve()
    target_root = Path(args.target).expanduser().resolve()

    if not source_root.is_dir():
        print(f"Error: source '{source_root}' is not a directory.")
        sys.exit(1)

    label = "[DRY RUN] " if args.dry_run else ""
    print(f"{label}Source: {source_root}")
    print(f"{label}Target: {target_root}\n")

    if not args.dry_run:
        target_root.mkdir(parents=True, exist_ok=True)

    # --- collect ---
    flac_files, mp3_files = collect(source_root, excludes)

    # Safety: an empty source against a populated mirror is almost always an
    # unmounted drive or wrong path — orphan removal would wipe the mirror.
    if not flac_files and not mp3_files:
        mirror_has_audio = target_root.exists() and any(target_root.rglob("*.mp3"))
        if mirror_has_audio:
            print("Error: source contains no audio but the target mirror is non-empty.\n"
                  "Refusing to delete the mirror. (Is the source drive mounted?)")
            sys.exit(1)

    # Safety: a derived MP3 mirror never contains FLACs. If the target does,
    # it is probably a real library — refuse to delete anything from it.
    if target_root.exists() and any(target_root.rglob("*.flac")):
        print(f"Error: target '{target_root}' contains FLAC files — that looks like a\n"
              "library, not a derived MP3 mirror. Refusing to sync into it.")
        sys.exit(1)

    # --- orphan removal ---
    if target_root.exists():
        removed = remove_orphans(source_root, target_root, excludes) if not args.dry_run else []
        if removed:
            print(f"Removed {len(removed)} orphaned file(s):")
            for p in sorted(removed):
                print(f"  - {p.relative_to(target_root)}")
            print()

    # --- image/lyrics sync ---
    if not args.dry_run:
        sync_images(source_root, target_root, excludes)

    # Song.flac and Song.mp3 in the same dir both target Song.mp3 — two
    # concurrent writers on one file. Prefer the FLAC (better source).
    flac_targets = {target_path(f, source_root, target_root) for f in flac_files}
    clashing = [m for m in mp3_files
                if target_path(m, source_root, target_root) in flac_targets]
    if clashing:
        print(f"WARNING: {len(clashing)} mp3(s) share a target with a flac of the "
              f"same name — converting the flac, skipping the mp3 copy:")
        for m in sorted(clashing):
            print(f"  - {m.relative_to(source_root)}")
        mp3_files = [m for m in mp3_files if m not in set(clashing)]

    flacs_to_convert = [f for f in flac_files if needs_update(f, target_path(f, source_root, target_root))]
    mp3s_to_copy     = [f for f in mp3_files  if needs_update(f, target_path(f, source_root, target_root))]

    print(f"FLACs:  {len(flac_files)} total  |  {len(flac_files) - len(flacs_to_convert)} skipped  |  {len(flacs_to_convert)} to convert")
    print(f"MP3s:   {len(mp3_files)} total  |  {len(mp3_files) - len(mp3s_to_copy)} skipped  |  {len(mp3s_to_copy)} to copy\n")

    if args.dry_run:
        for f in sorted(flacs_to_convert):
            print(f"  would convert: {f.relative_to(source_root)}")
        for f in sorted(mp3s_to_copy):
            print(f"  would copy:    {f.relative_to(source_root)}")
        return

    if not flacs_to_convert and not mp3s_to_copy:
        print("Nothing to do.")
        return

    errors = []
    done = 0
    total = len(flacs_to_convert) + len(mp3s_to_copy)

    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {}
        for f in flacs_to_convert:
            futures[executor.submit(convert, f, target_path(f, source_root, target_root), args.bitrate)] = ("convert", f)
        for f in mp3s_to_copy:
            futures[executor.submit(copy_file, f, target_path(f, source_root, target_root))] = ("copy", f)

        for future in as_completed(futures):
            action, _ = futures[future]
            src, ok, error = future.result()
            done += 1
            status = "OK" if ok else "FAIL"
            verb   = "convert" if action == "convert" else "copy   "
            print(f"  [{done}/{total}] {status}  {verb}  {src.relative_to(source_root)}")
            if not ok:
                errors.append((src, error))

    print(f"\n{'─' * 60}")
    print(f"Converted: {len(flacs_to_convert) - sum(1 for s, _ in errors if s.suffix == '.flac')}")
    print(f"Copied:    {len(mp3s_to_copy) - sum(1 for s, _ in errors if s.suffix == '.mp3')}")
    print(f"Failed:    {len(errors)}")

    if errors:
        print("\nFailed files:")
        for src, error in sorted(errors):
            print(f"  {src.relative_to(source_root)}")
            if error:
                for line in error.splitlines()[-3:]:
                    print(f"    {line}")
        sys.exit(1)


if __name__ == "__main__":
    main()
