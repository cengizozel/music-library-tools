#!/usr/bin/env python3
"""
Checks every FLAC file in a library for audio corruption.
Uses `flac --test` which fully decodes each file and verifies its MD5 checksum.

Requires: flac CLI  (brew install flac)
"""

import os
import sys
import shutil
import argparse
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


def check_flac(path: Path) -> tuple[Path, bool, str]:
    result = subprocess.run(
        ["flac", "--test", "--silent", str(path)],
        capture_output=True,
        text=True,
    )
    ok = result.returncode == 0
    error = result.stderr.strip() if not ok else ""
    return path, ok, error


def scan(library_path: Path, jobs: int) -> tuple[list, list]:
    flac_files = [
        Path(root) / fname
        for root, _, files in os.walk(library_path)
        for fname in files
        if fname.lower().endswith(".flac")
    ]

    total = len(flac_files)
    if total == 0:
        print("No FLAC files found.")
        return [], []

    print(f"Testing {total} file(s) with {jobs} thread(s)...\n")

    ok_files = []
    corrupt_files = []
    done = 0

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {executor.submit(check_flac, p): p for p in flac_files}
        for future in as_completed(futures):
            path, ok, error = future.result()
            done += 1
            status = "OK" if ok else "CORRUPT"
            print(f"  [{done}/{total}] {status}  {path.name}")
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
    parser = argparse.ArgumentParser(description="Check FLAC files for audio corruption.")
    parser.add_argument("library", help="Path to the music library directory")
    parser.add_argument("--jobs", type=int, default=4,
                        help="Number of parallel threads (default: 4)")
    args = parser.parse_args()

    if not shutil.which("flac"):
        print("Error: 'flac' CLI not found. Install it with: brew install flac")
        sys.exit(1)

    library_path = Path(args.library).expanduser().resolve()
    if not library_path.is_dir():
        print(f"Error: '{library_path}' is not a directory.")
        sys.exit(1)

    print(f"Scanning: {library_path}\n")
    ok_files, corrupt_files = scan(library_path, args.jobs)
    report(ok_files, corrupt_files)

    if corrupt_files:
        sys.exit(1)


if __name__ == "__main__":
    main()
