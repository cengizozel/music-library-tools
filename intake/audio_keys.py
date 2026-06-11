#!/usr/bin/env python3
"""
Content-based identity keys for audio files and albums.

Keys are path-independent and retag-proof, so decisions made about a file are
found again no matter where the file lives or how its tags changed since:

  FLAC   flac:<STREAMINFO MD5>      audio MD5 written by the encoder; survives retagging
  MP3    mp3:<sha1 of raw frames>   file bytes minus ID3v2 header, ID3v1 and APEv2 tails
  other  pcm:<md5>                  ffmpeg-decoded audio MD5 (tag-independent, needs decode)
  last   file:<sha1>                whole-file hash if everything else fails

Album key: album:<sha1 of newline-joined sorted track keys>.
"""

import hashlib
import subprocess
from pathlib import Path

ZERO_MD5 = "0" * 32


def _flac_streaminfo_md5(path: Path) -> str | None:
    result = subprocess.run(
        ["metaflac", "--show-md5sum", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    md5 = result.stdout.strip().lower()
    if len(md5) == 32 and md5 != ZERO_MD5:
        return md5
    return None


def _id3v2_size(head: bytes) -> int:
    """Total byte size of an ID3v2 header block, 0 if absent."""
    if len(head) < 10 or head[:3] != b"ID3":
        return 0
    if head[5] & 0x10:  # footer present flag
        footer = 10
    else:
        footer = 0
    size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
    return 10 + size + footer


def _mp3_stream_sha1(path: Path) -> str | None:
    """sha1 of the file minus leading ID3v2 and trailing ID3v1/APEv2 tags."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(10)
            start = _id3v2_size(head)
            end = size
            # ID3v1: last 128 bytes start with "TAG"
            if end - start >= 128:
                f.seek(end - 128)
                if f.read(3) == b"TAG":
                    end -= 128
            # APEv2 footer: 32 bytes ending region starts with "APETAGEX"
            if end - start >= 32:
                f.seek(end - 32)
                footer = f.read(32)
                if footer[:8] == b"APETAGEX":
                    tag_size = int.from_bytes(footer[12:16], "little")
                    has_header = bool(footer[20:24][3] & 0x80)
                    total = tag_size + (32 if has_header else 0)
                    if total < end - start:
                        end -= total
            if end <= start:
                return None
            h = hashlib.sha1()
            f.seek(start)
            remaining = end - start
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
            return h.hexdigest()
    except OSError:
        return None


def _ffmpeg_pcm_md5(path: Path) -> str | None:
    result = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", str(path), "-map", "0:a:0", "-f", "md5", "-"],
        capture_output=True, text=True,
    )
    out = result.stdout.strip()
    if result.returncode == 0 and out.startswith("MD5="):
        return out[4:].lower()
    return None


def _file_sha1(path: Path) -> str | None:
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def track_key(path: Path) -> str | None:
    """Content key for one audio file, or None if the file is unreadable."""
    ext = path.suffix.lower()
    if ext == ".flac":
        md5 = _flac_streaminfo_md5(path)
        if md5:
            return f"flac:{md5}"
        pcm = _ffmpeg_pcm_md5(path)
        if pcm:
            return f"pcm:{pcm}"
    elif ext == ".mp3":
        sha = _mp3_stream_sha1(path)
        if sha:
            return f"mp3:{sha}"
    else:
        pcm = _ffmpeg_pcm_md5(path)
        if pcm:
            return f"pcm:{pcm}"
    sha = _file_sha1(path)
    return f"file:{sha}" if sha else None


def album_key(track_keys: list[str]) -> str:
    joined = "\n".join(sorted(track_keys))
    return "album:" + hashlib.sha1(joined.encode()).hexdigest()
