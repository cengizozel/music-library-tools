#!/usr/bin/env python3
"""
Checks every FLAC and MP3 file in a library for audio corruption.

  - FLACs: uses `flac --test` (full decode + MD5 checksum verification)
  - MP3s:  uses `ffmpeg -v error` (decode error detection)

Requires:
  flac   (brew install flac)    — only needed if library contains FLACs
  ffmpeg (brew install ffmpeg)  — only needed if library contains MP3s
"""

import os
import sys
import shutil
import argparse
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

AUDIO_EXTENSIONS = {".flac", ".mp3"}


def check_flac(path: Path) -> tuple[Path, bool, str]:
    result = subprocess.run(
        ["flac", "--test", "--silent", str(path)],
        capture_output=True,
        text=True,
    )
    ok = result.returncode == 0
    error = result.stderr.strip() if not ok else ""
    return path, ok, error


def check_mp3(path: Path) -> tuple[Path, bool, str]:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    error = result.stderr.strip()
    return path, not error, error


def check_audio(path: Path) -> tuple[Path, bool, str]:
    if path.suffix.lower() == ".flac":
        return check_flac(path)
    return check_mp3(path)


def collect(library_path: Path) -> tuple[list[Path], list[Path]]:
    flacs, mp3s = [], []
    for root, _, files in os.walk(library_path):
        for fname in files:
            p = Path(root) / fname
            ext = p.suffix.lower()
            if ext == ".flac":
                flacs.append(p)
            elif ext == ".mp3":
                mp3s.append(p)
    return flacs, mp3s


def scan(library_path: Path, jobs: int) -> tuple[list, list]:
    flacs, mp3s = collect(library_path)
    all_files = flacs + mp3s
    total = len(all_files)

    if total == 0:
        print("No audio files found.")
        return [], []

    print(f"FLACs: {len(flacs)}  |  MP3s: {len(mp3s)}  |  Threads: {jobs}\n")

    ok_files = []
    corrupt_files = []
    done = 0

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {executor.submit(check_audio, p): p for p in all_files}
        for future in as_completed(futures):
            path, ok, error = future.result()
            done += 1
            status = "OK     " if ok else "CORRUPT"
            print(f"  [{done}/{total}] {status}  {path.relative_to(library_path)}")
            if ok:
                ok_files.append(path)
            else:
                corrupt_files.append((path, error))

    return ok_files, corrupt_files


def report(ok_files: list, corrupt_files: list):
    total = len(ok_files) + len(corrupt_files)
    print(f"\n{'─' * 60}")
    print(f"Scanned:  {total}")
    print(f"OK:       {len(ok_files)}")
    print(f"Corrupt:  {len(corrupt_files)}")

    if corrupt_files:
        print(f"\nCorrupt files:")
        for path, error in sorted(corrupt_files):
            print(f"  {path}")
            if error:
                for line in error.splitlines():
                    print(f"    {line}")


def main():
    parser = argparse.ArgumentParser(description="Check audio files for corruption.")
    parser.add_argument("library", help="Path to the music library directory")
    parser.add_argument("--jobs", type=int, default=4,
                        help="Number of parallel threads (default: 4)")
    args = parser.parse_args()

    library_path = Path(args.library).expanduser().resolve()
    if not library_path.is_dir():
        print(f"Error: '{library_path}' is not a directory.")
        sys.exit(1)

    flacs, mp3s = collect(library_path)

    if flacs and not shutil.which("flac"):
        print("Error: 'flac' CLI not found (needed for FLAC files). Install: brew install flac")
        sys.exit(1)
    if mp3s and not shutil.which("ffmpeg"):
        print("Error: 'ffmpeg' not found (needed for MP3 files). Install: brew install ffmpeg")
        sys.exit(1)

    print(f"Scanning: {library_path}\n")
    ok_files, corrupt_files = scan(library_path, args.jobs)
    report(ok_files, corrupt_files)

    if corrupt_files:
        sys.exit(1)


if __name__ == "__main__":
    main()
