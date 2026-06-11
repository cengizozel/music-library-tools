#!/usr/bin/env python3
"""
Deletes junk files from a directory tree before import — .nfo, .log, .m3u,
.txt, Thumbs.db, etc. Also removes empty directories left behind.

KEPT (same policy as the intake pipeline):
  - any audio format (never deletes audio, even formats the library
    doesn't use — those are decisions, not junk)
  - .lrc lyrics, cover images (.jpg/.jpeg/.png)
  - .pdf booklets and .cue sheets (useful for identifying releases)
  - .keep markers; everything under a _Corrupt quarantine dir

Dry-run by default. Use --apply to delete.

Usage:
  python3 strip_non_audio/strip.py /path/to/incoming
  python3 strip_non_audio/strip.py /path/to/incoming --apply
"""

import os
import sys
import argparse
from pathlib import Path

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".wav", ".aiff", ".aif", ".ogg",
                    ".oga", ".opus", ".wma", ".ape", ".wv", ".alac", ".aac",
                    ".mpc", ".tak", ".shn", ".dsf", ".dff", ".dts", ".m4b", ".mka"}
COMPANION_EXTENSIONS = {".lrc", ".jpg", ".jpeg", ".png", ".bmp", ".gif",
                        ".webp", ".tif", ".tiff", ".pdf", ".cue"}
KEEP_NAMES = {".keep"}
QUARANTINE = "_Corrupt"


def main():
    parser = argparse.ArgumentParser(
        description="Remove junk (non-audio, non-companion) files from an incoming music directory."
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

    kept_ext = AUDIO_EXTENSIONS | COMPANION_EXTENSIONS
    to_delete = []
    for root, dirs, files in os.walk(directory):
        rp = Path(root)
        if QUARANTINE in rp.relative_to(directory).parts:
            dirs[:] = []
            continue
        for fname in files:
            p = rp / fname
            if p.suffix.lower() not in kept_ext and fname not in KEEP_NAMES:
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
            if p == directory or QUARANTINE in p.relative_to(directory).parts:
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
