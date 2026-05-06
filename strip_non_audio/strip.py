#!/usr/bin/env python3
"""
Deletes all non-audio files from a directory tree before import.
Removes .log, .m3u, .txt, .jpg, .db, and any other non-FLAC/MP3 files.
Also removes empty directories left behind.

Dry-run by default. Use --apply to delete.

Usage:
  python3 strip_non_audio/strip.py /path/to/incoming
  python3 strip_non_audio/strip.py /path/to/incoming --apply
"""

import os
import sys
import argparse
from pathlib import Path

AUDIO_EXTENSIONS = {".flac", ".mp3"}


def main():
    parser = argparse.ArgumentParser(
        description="Remove non-audio files from an incoming music directory."
    )
    parser.add_argument("directory", help="Path to the incoming directory")
    parser.add_argument("--apply", action="store_true",
                        help="Apply deletions (default is dry-run)")
    args = parser.parse_args()

    directory = Path(args.directory).expanduser().resolve()
    if not directory.is_dir():
        print(f"Error: '{directory}' is not a directory.")
        sys.exit(1)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] Scanning: {directory}\n")

    to_delete = []
    for root, dirs, files in os.walk(directory):
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() not in AUDIO_EXTENSIONS and fname != ".keep":
                to_delete.append(p)

    if not to_delete:
        print("Nothing to remove.")
        return

    for p in sorted(to_delete):
        print(f"  {'DELETE' if args.apply else 'would delete'}  {p.relative_to(directory)}")
        if args.apply:
            p.unlink()

    # Remove empty directories
    if args.apply:
        for root, dirs, files in os.walk(directory, topdown=False):
            p = Path(root)
            if p == directory:
                continue
            try:
                p.rmdir()
            except OSError:
                pass

    print(f"\n{'Deleted' if args.apply else 'Would delete'}: {len(to_delete)} file(s)")
    if not args.apply:
        print("Run with --apply to delete.")


if __name__ == "__main__":
    main()
