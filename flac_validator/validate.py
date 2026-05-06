#!/usr/bin/env python3
"""
Scans a music library directory and flags any file that is not a valid FLAC.
Checks both file extension and actual file header (magic bytes).
"""

import os
import sys
import argparse
from pathlib import Path

AUDIO_EXTENSIONS = {".flac", ".mp3", ".aac", ".m4a", ".wav", ".aiff", ".aif",
                    ".ogg", ".opus", ".wma", ".alac", ".ape", ".wv"}

FLAC_MAGIC = b"fLaC"


def is_flac_by_header(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == FLAC_MAGIC
    except (OSError, IOError):
        return False


def scan(library_path: Path) -> dict:
    results = {
        "wrong_extension": [],   # non-.flac extension but valid FLAC header
        "non_flac_audio": [],    # audio extension, not a FLAC
        "fake_flac": [],         # .flac extension but wrong header
        "unreadable": [],        # couldn't read the file
    }

    for root, _, files in os.walk(library_path):
        for fname in files:
            path = Path(root) / fname
            ext = path.suffix.lower()

            if ext not in AUDIO_EXTENSIONS:
                continue

            try:
                header_is_flac = is_flac_by_header(path)
            except Exception:
                results["unreadable"].append(path)
                continue

            if ext == ".flac" and not header_is_flac:
                results["fake_flac"].append(path)
            elif ext != ".flac" and header_is_flac:
                results["wrong_extension"].append(path)
            elif ext != ".flac" and not header_is_flac:
                results["non_flac_audio"].append(path)

    return results


def report(results: dict, library_path: Path):
    total_issues = sum(len(v) for v in results.values())

    if total_issues == 0:
        print("All audio files are valid FLACs.")
        return

    print(f"Found {total_issues} issue(s) in: {library_path}\n")

    if results["non_flac_audio"]:
        print(f"  Non-FLAC audio files ({len(results['non_flac_audio'])}):")
        for p in sorted(results["non_flac_audio"]):
            print(f"    {p}")

    if results["fake_flac"]:
        print(f"\n  .flac files with invalid header ({len(results['fake_flac'])}):")
        for p in sorted(results["fake_flac"]):
            print(f"    {p}")

    if results["wrong_extension"]:
        print(f"\n  Valid FLAC with wrong extension ({len(results['wrong_extension'])}):")
        for p in sorted(results["wrong_extension"]):
            print(f"    {p}")

    if results["unreadable"]:
        print(f"\n  Unreadable files ({len(results['unreadable'])}):")
        for p in sorted(results["unreadable"]):
            print(f"    {p}")


def main():
    parser = argparse.ArgumentParser(description="Validate that all audio files in a library are FLAC.")
    parser.add_argument("library", help="Path to the music library directory")
    args = parser.parse_args()

    library_path = Path(args.library).expanduser().resolve()

    if not library_path.is_dir():
        print(f"Error: '{library_path}' is not a directory.")
        sys.exit(1)

    print(f"Scanning: {library_path}\n")
    results = scan(library_path)
    report(results, library_path)


if __name__ == "__main__":
    main()
