#!/usr/bin/env python3
"""
intake — one command to process everything in _Staging.

    venv/bin/python intake/intake.py                 # interactive (default)
    venv/bin/python intake/intake.py --auto          # no questions: apply stored decisions, defer the rest
    venv/bin/python intake/intake.py --dry-run       # report what would happen
    venv/bin/python intake/intake.py --sync          # also run the MP3 mirror sync at the end

Phases: strip junk -> validate/classify audio -> corruption check -> group into
album units -> replay remembered decisions -> beets quiet import -> interactive
resolution -> albumartist normalization -> companion files -> summary -> mp3 sync.

Every manual answer is remembered in <library>/.music-tools/decisions.json keyed
by audio content, so a re-staged album (even after a future full restructure)
never asks the same question twice. See DESIGN.md.

Exit codes: 0 staging fully processed, 1 error, 2 finished but staging still
holds audio (deferred questions, skipped or held-back units).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audio_keys import track_key, album_key
from decision_store import DecisionStore
from artist_normalizer import detect, fold
from beets_runner import BeetsRunner

REPO_ROOT = Path(__file__).resolve().parent.parent

CORE_AUDIO = {".flac", ".mp3"}
OTHER_AUDIO = {".m4a", ".wav", ".aiff", ".aif", ".ogg", ".oga", ".opus", ".wma",
               ".ape", ".wv", ".alac", ".aac", ".mpc", ".tak", ".shn", ".dsf",
               ".dff", ".dts", ".m4b", ".mka"}
LOSSLESS_OTHER = {".wav", ".aiff", ".aif", ".ape", ".wv", ".alac", ".tak",
                  ".shn", ".dsf", ".dff"}
ALL_AUDIO = CORE_AUDIO | OTHER_AUDIO
COMPANION = {".lrc"}
IMAGES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
KEEP_NAMES = {".keep"}
# Useful for identifying a release that fails to match (booklets, cue sheets).
# They stay with the unit through import and are cleaned up only after the
# album lands in the library; unresolved albums keep them in staging.
KEEP_UNTIL_RESOLVED = {".pdf", ".cue"}

# Bare disc subfolders are merged into their parent album unit. Named discs
# ("CD2 - Relapse Refill") stay separate: they are usually their own release.
DISC_DIR_RE = re.compile(r'^(?:\d+["”]?\s+)?(?:cd|disc|disk|vinyl|lp)\s*0*(\d+)$', re.IGNORECASE)

QUARANTINE = "_Corrupt"


# --------------------------------------------------------------------------- ui

class Deferred(Exception):
    """Raised in --auto mode when a question would be needed."""


class UI:
    def __init__(self, auto: bool, dry_run: bool):
        self.auto = auto
        self.dry_run = dry_run
        self.deferred: list[str] = []
        self.answered = 0

    def say(self, msg: str = ""):
        print(msg)

    def head(self, msg: str):
        print(f"\n{'─' * 70}\n{msg}\n{'─' * 70}")

    def ask(self, question: str, choices: dict[str, str], default: str,
            context: str = "") -> str:
        """choices: {key: label}. Returns chosen key. Raises Deferred in --auto."""
        if self.auto or self.dry_run:
            self.deferred.append(context or question)
            raise Deferred(question)
        menu = "  ".join(f"[{k}]{'*' if k == default else ''} {v}" for k, v in choices.items())
        while True:
            try:
                raw = input(f"{question}\n  {menu}\n  > ").strip().lower()
            except EOFError:
                self.deferred.append(context or question)
                raise Deferred(question)
            if not raw:
                raw = default
            if raw in choices:
                self.answered += 1
                return raw
            print(f"  ('{raw}' is not an option)")

    def ask_text(self, question: str, default: str = "", context: str = "") -> str:
        if self.auto or self.dry_run:
            self.deferred.append(context or question)
            raise Deferred(question)
        try:
            raw = input(f"{question} [{default}]\n  > ").strip()
        except EOFError:
            self.deferred.append(context or question)
            raise Deferred(question)
        self.answered += 1
        return raw or default


# ------------------------------------------------------------------- sniffing

def sniff(path: Path) -> str:
    """'flac' | 'flac_id3' (FLAC with illegally prepended ID3v2) | 'mp3' | 'unknown'"""
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            if head[:4] == b"fLaC":
                return "flac"
            if head[:3] == b"ID3" and len(head) >= 10:
                size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
                f.seek(10 + size)
                after = f.read(4096) or b""
                if after[:4] == b"fLaC":
                    return "flac_id3"
                # skip padding some taggers leave after the ID3 block
                idx = after.find(b"fLaC")
                if 0 <= idx < 4000:
                    return "flac_id3"
                return "mp3"
            if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
                return "mp3"
    except OSError:
        pass
    return "unknown"


def repair_flac(path: Path) -> bool:
    """Make a .flac standards-clean: drop an illegally prepended ID3v2 block and
    trailing ID3v1/APEv2 tags (all of which break strict decoders). The FLAC
    stream itself is untouched, so its STREAMINFO MD5 (our content key) is
    stable across the repair. True if the file passes `flac --test` afterwards."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    start = 0
    if data[:3] == b"ID3" and len(data) > 10:
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        cand = 10 + size
        if data[cand:cand + 4] == b"fLaC":
            start = cand
        else:
            idx = data.find(b"fLaC")
            if idx < 0:
                return False
            start = idx
    elif data[:4] != b"fLaC":
        return False
    end = len(data)
    if end - start > 128 and data[end - 128:end - 125] == b"TAG":
        end -= 128
    if end - start > 32 and data[end - 32:end - 24] == b"APETAGEX":
        tag_size = int.from_bytes(data[end - 20:end - 16], "little")
        # flags are LE at footer offset 20..23; the has-header bit is bit 31,
        # i.e. the high bit of the LAST flags byte (end-9), not the first
        has_header = bool(data[end - 9] & 0x80)
        total = tag_size + (32 if has_header else 0)
        if 0 < total < end - start:
            end -= total
    if start == 0 and end == len(data):
        return False  # nothing strippable: corruption is elsewhere
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data[start:end])
        result = subprocess.run(["flac", "--test", "--silent", str(tmp)],
                                capture_output=True)
        if result.returncode != 0:
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, path)
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False


def read_tags(path: Path) -> dict:
    """Lowercased tag dict via mutagen (easy interface where possible)."""
    try:
        import mutagen
        audio = mutagen.File(path, easy=True)
        if audio is None or not audio.tags:
            return {}
        out = {}
        for k, v in audio.tags.items():
            if isinstance(v, list) and v:
                out[k.lower()] = str(v[0])
            else:
                out[k.lower()] = str(v)
        return out
    except Exception:
        return {}


def parse_intlike(raw: str) -> int:
    try:
        return int(str(raw).split("/")[0])
    except (ValueError, AttributeError):
        return 0


def sanitize(name: str, max_bytes: int = 180) -> str:
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = name.strip(". ") or "_"
    # FAT (iPod/SD) caps names at 255 bytes; classical movement titles get long.
    # Clamp at a UTF-8 character boundary, leaving room for 'NNN - ' and '.flac'.
    while len(name.encode("utf-8")) > max_bytes:
        name = name[:-1].rstrip(". ") or "_"
    return name


def unique_path(p: Path) -> Path:
    """p itself if free, else 'stem (N).suffix' — never overwrite anything."""
    if not p.exists():
        return p
    n = 1
    while True:
        cand = p.with_name(f"{p.stem} ({n}){p.suffix}")
        if not cand.exists():
            return cand
        n += 1


# --------------------------------------------------------------------- units

@dataclass
class Unit:
    """One album-shaped group of audio files in staging."""
    dir: Path
    files: list[Path] = field(default_factory=list)   # audio files
    keys: dict[Path, str] = field(default_factory=dict)
    loose: bool = False

    @property
    def key(self) -> str | None:
        ks = [k for k in self.keys.values() if k]
        return album_key(ks) if ks else None

    def audio_left(self) -> list[Path]:
        if self.loose:
            # staging root: only direct children — never sweep up other units
            return [f for f in self.dir.iterdir()
                    if f.is_file() and f.suffix.lower() in ALL_AUDIO]
        return [f for f in self.dir.rglob("*") if f.is_file() and f.suffix.lower() in ALL_AUDIO]


def discover_units(staging: Path) -> list[Unit]:
    units: dict[Path, Unit] = {}
    for root, dirs, files in os.walk(staging):
        rp = Path(root)
        if QUARANTINE in rp.relative_to(staging).parts:
            dirs[:] = []
            continue
        audio = sorted(rp / f for f in files if Path(f).suffix.lower() in ALL_AUDIO)
        if not audio:
            continue
        if rp == staging:
            unit = units.setdefault(rp, Unit(dir=rp, loose=True))
        elif DISC_DIR_RE.match(rp.name) and rp.parent != staging:
            # disc folder inside an album dir merges into the parent; a disc
            # folder directly at staging root must NOT make the root a unit
            unit = units.setdefault(rp.parent, Unit(dir=rp.parent))
        else:
            unit = units.setdefault(rp, Unit(dir=rp))
        unit.files.extend(audio)
    return sorted(units.values(), key=lambda u: str(u.dir))


# ------------------------------------------------------------------ pipeline

class Pipeline:
    def __init__(self, cfg: dict, args):
        self.library = Path(cfg["library"]).expanduser().resolve()
        self.staging = Path(cfg.get("staging", self.library / "_Staging")).expanduser().resolve()
        self.mp3_target = cfg.get("mp3_target", "")
        self.bitrate = cfg.get("bitrate", "320k")
        self.jobs = int(cfg.get("jobs", 4))
        self.auto_sync = bool(cfg.get("auto_sync", False))
        self.args = args
        self.ui = UI(auto=args.auto, dry_run=args.dry_run)

        tools_dir = self.library / ".music-tools"
        self.store = DecisionStore(tools_dir / "decisions.json")
        self.lock_path = tools_dir / "intake.lock"

        beets_config = Path(cfg.get("beets_config", REPO_ROOT / "beets" / "config.yaml"))
        venv_bin = Path(sys.executable).parent
        fpcalc = venv_bin / "fpcalc"
        self.runner = BeetsRunner(
            beet_bin=venv_bin / "beet",
            base_config=beets_config,
            library_dir=self.library,
            db_path=Path(cfg.get("beets_db", tools_dir / "beets.db")),
            fpcalc=fpcalc if fpcalc.exists() else None,
            log_path=tools_dir / "import.log",
        )

        self.stats: dict[str, int] = {}
        self.imported_albums: list[dict] = []   # beets album dicts imported this run
        self.seen_album_ids: set[str] = set()   # every album id observed this run,
                                                # incl. ones from failed imports
        self.deleted_files: list[Path] = []
        self.held: set[Path] = set()            # files with unresolved problems:
                                                # their whole unit is held back from import

    # --- helpers ---

    def bump(self, stat: str, n: int = 1):
        self.stats[stat] = self.stats.get(stat, 0) + n

    def known_artists(self) -> set[str]:
        """Artist names for split-suggestion preference (library dirs + map values)."""
        dirs = {d.name for d in self.library.iterdir()
                if d.is_dir() and not d.name.startswith((".", "_"))}
        dirs.update(self.store.data["artist_map"].values())
        return dirs

    def confirmed_artists(self) -> set[str]:
        """Values the user explicitly confirmed as single artists (identity maps)."""
        return {raw for raw, canon in self.store.data["artist_map"].items()
                if raw == fold(canon)}

    def delete(self, path: Path, why: str):
        rel = path.relative_to(self.staging)
        if self.args.dry_run:
            self.ui.say(f"  would delete ({why}): {rel}")
        else:
            path.unlink()
            self.ui.say(f"  deleted ({why}): {rel}")
        self.deleted_files.append(path)
        self.bump("deleted")

    # --- phase 1: strip ---

    def phase_strip(self):
        self.ui.head("Phase 1/8 · Strip non-audio junk")
        kept_ext = ALL_AUDIO | COMPANION | IMAGES | KEEP_UNTIL_RESOLVED
        junk = []
        for root, dirs, files in os.walk(self.staging):
            rp = Path(root)
            if QUARANTINE in rp.relative_to(self.staging).parts:
                dirs[:] = []
                continue
            for f in files:
                p = rp / f
                if p.suffix.lower() not in kept_ext and f not in KEEP_NAMES:
                    junk.append(p)
        for p in sorted(junk):
            self.delete(p, "junk")
        self._remove_empty_dirs()
        if not junk:
            self.ui.say("  nothing to strip")

    def _remove_empty_dirs(self):
        if self.args.dry_run:
            return
        for root, dirs, files in os.walk(self.staging, topdown=False):
            rp = Path(root)
            if rp == self.staging or QUARANTINE in rp.parts:
                continue
            try:
                rp.rmdir()
            except OSError:
                pass

    # --- phase 2: validate / classify ---

    def phase_validate(self):
        self.ui.head("Phase 2/8 · Validate & classify audio")
        actions = 0
        for root, dirs, files in os.walk(self.staging):
            rp = Path(root)
            if QUARANTINE in rp.relative_to(self.staging).parts:
                dirs[:] = []
                continue
            for f in sorted(files):
                p = rp / f
                ext = p.suffix.lower()
                if ext not in ALL_AUDIO:
                    continue
                if p.stat().st_size == 0:
                    actions += self._handle_zero_byte(p)
                    continue
                if ext == ".flac" or ext == ".mp3":
                    actions += self._validate_core(p)
        # non-flac/mp3 audio is decided per directory (one question per album)
        actions += self._handle_other_audio()
        if not actions:
            self.ui.say("  all audio files look sane")

    def _handle_zero_byte(self, p: Path) -> int:
        try:
            choice = self.ui.ask(f"Zero-byte file: {p.relative_to(self.staging)}",
                                 {"d": "delete", "s": "skip"}, "d", context=str(p))
        except Deferred:
            self.held.add(p)
            return 1
        if choice == "d":
            self.delete(p, "zero-byte")
        else:
            self.held.add(p)
        return 1

    def _validate_core(self, p: Path) -> int:
        ext = p.suffix.lower()
        kind = sniff(p)
        rel = p.relative_to(self.staging)
        if ext == ".flac" and kind == "flac":
            return 0
        if ext == ".flac" and kind == "flac_id3":
            if self.args.dry_run:
                self.ui.say(f"  would strip prepended ID3 block: {rel}")
                return 1
            if repair_flac(p):
                self.ui.say(f"  fixed: stripped illegal ID3 wrapping: {rel}")
                self.bump("flac_repaired")
            else:
                self.ui.say(f"  WARNING could not fix ID3-wrapped flac (held back): {rel}")
                self.held.add(p)
            return 1
        if ext == ".flac" and kind == "mp3":
            try:
                choice = self.ui.ask(
                    f"'{rel}' has a .flac extension but contains MP3 data.",
                    {"r": "rename to .mp3 (and allow)", "d": "delete", "s": "skip"},
                    "r", context=str(p))
            except Deferred:
                self.held.add(p)
                return 1
            if choice == "s":
                self.held.add(p)
            if choice == "r" and not self.args.dry_run:
                new = unique_path(p.with_suffix(".mp3"))
                p.rename(new)
                key = track_key(new)
                if key:
                    self.store.set_track(key, allowed_mp3=True, note="was fake flac")
                self.ui.say(f"  renamed -> {new.name}")
                self.bump("renamed")
            elif choice == "d":
                self.delete(p, "fake flac")
            return 1
        if ext == ".mp3" and kind == "flac":
            if not self.args.dry_run:
                new = unique_path(p.with_suffix(".flac"))
                p.rename(new)
                self.ui.say(f"  renamed (FLAC data): {rel} -> {new.name}")
            else:
                self.ui.say(f"  would rename (FLAC data): {rel} -> .flac")
            self.bump("renamed")
            return 1
        if ext == ".mp3" and kind == "mp3":
            return 0  # acceptability is decided per album unit during resolution
        if kind == "unknown":
            try:
                choice = self.ui.ask(
                    f"'{rel}' is not recognizable audio (corrupt header?).",
                    {"d": "delete", "s": "skip"}, "s", context=str(p))
            except Deferred:
                self.held.add(p)
                return 1
            if choice == "d":
                self.delete(p, "unrecognized")
            else:
                self.held.add(p)
            return 1
        return 0

    def _handle_other_audio(self) -> int:
        by_dir: dict[Path, list[Path]] = {}
        for root, dirs, files in os.walk(self.staging):
            rp = Path(root)
            if QUARANTINE in rp.relative_to(self.staging).parts:
                dirs[:] = []
                continue
            for f in sorted(files):
                p = rp / f
                if p.suffix.lower() in OTHER_AUDIO:
                    by_dir.setdefault(rp, []).append(p)
        actions = 0
        for d, paths in sorted(by_dir.items()):
            # honor remembered "keep as-is" decisions before asking anything
            kept_before = [p for p in paths
                           if self.store.track(track_key(p) or "").get("format") == "keep"]
            if kept_before:
                self.ui.say(f"  (previously allowed) {len(kept_before)} non-core "
                            f"audio file(s) in {d.relative_to(self.staging) if d != self.staging else '.'}")
                paths = [p for p in paths if p not in kept_before]
                if not paths:
                    continue
            exts = sorted({p.suffix.lower() for p in paths})
            rel = d.relative_to(self.staging) if d != self.staging else Path(".")
            lossless = all(e in LOSSLESS_OTHER for e in exts)
            if lossless:
                choices = {"c": "convert to FLAC (lossless)", "k": "keep as-is (allow)",
                           "d": "delete", "s": "skip"}
                default = "c"
            else:
                choices = {"k": "keep as-is (allow)", "m": "convert to MP3 320k",
                           "d": "delete", "s": "skip"}
                default = "k"
            try:
                choice = self.ui.ask(
                    f"'{rel}' contains {len(paths)} non-FLAC/MP3 audio file(s) ({', '.join(exts)}).",
                    choices, default, context=str(d))
            except Deferred:
                self.held.update(paths)
                actions += 1
                continue
            actions += 1
            for p in paths:
                if choice == "c":
                    self._convert(p, ".flac", ["-c:a", "flac"])
                elif choice == "m":
                    self._convert(p, ".mp3", ["-codec:a", "libmp3lame", "-b:a", self.bitrate])
                elif choice == "k":
                    key = track_key(p)
                    if key:
                        self.store.set_track(key, format="keep", note=f"kept {p.suffix}")
                elif choice == "d":
                    self.delete(p, "non-core audio")
                else:
                    self.held.add(p)
        return actions

    def _convert(self, src: Path, new_ext: str, codec_args: list[str]):
        if self.args.dry_run:
            self.ui.say(f"  would convert: {src.name} -> {src.with_suffix(new_ext).name}")
            return
        # never clobber an existing sibling (Song.wav next to a real Song.flac)
        dst = unique_path(src.with_suffix(new_ext))
        # -map 0:a drops embedded cover art; rescue it as a file first
        if not any(p.suffix.lower() in IMAGES for p in src.parent.iterdir()):
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(src), "-an", "-frames:v", "1",
                 str(src.parent / "cover.jpg")],
                capture_output=True)
            cov = src.parent / "cover.jpg"
            if cov.exists() and cov.stat().st_size == 0:
                cov.unlink()
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-map_metadata", "0", "-map", "0:a",
             *codec_args, str(dst)],
            capture_output=True, text=True)
        if result.returncode == 0:
            src.unlink()
            self.ui.say(f"  converted: {src.name} -> {dst.name}")
            self.bump("converted")
        else:
            dst.unlink(missing_ok=True)
            self.held.add(src)
            self.ui.say(f"  ERROR converting {src.name} (held back): {result.stderr.strip().splitlines()[-1] if result.stderr else 'unknown'}")

    # --- phase 3: corruption ---

    def phase_corruption(self):
        self.ui.head("Phase 3/8 · Corruption check")
        targets = []
        for root, dirs, files in os.walk(self.staging):
            rp = Path(root)
            if QUARANTINE in rp.relative_to(self.staging).parts:
                dirs[:] = []
                continue
            for f in sorted(files):
                p = rp / f
                if p.suffix.lower() in ALL_AUDIO and p.stat().st_size > 0:
                    targets.append(p)
        if not targets:
            self.ui.say("  no audio to check")
            return
        # content already decode-verified clean in an earlier pass never needs
        # re-decoding: hashing the header/stream is ~100x cheaper than decode
        cache_path = self.library / ".music-tools" / "clean-cache.json"
        try:
            clean_cache = set(json.loads(cache_path.read_text())) if cache_path.exists() else set()
        except (OSError, ValueError):
            clean_cache = set()
        if clean_cache:
            skipped = 0
            remaining = []
            with ThreadPoolExecutor(max_workers=self.jobs) as ex:
                for p, key in zip(targets, ex.map(track_key, targets)):
                    if key and key in clean_cache:
                        skipped += 1
                    else:
                        remaining.append(p)
            if skipped:
                self.ui.say(f"  {skipped} file(s) previously verified clean — skipped")
            targets = remaining
        if not targets:
            self.ui.say("  nothing new to decode")
            return
        corrupt: list[tuple[Path, str]] = []
        newly_clean: list[Path] = []
        checked = 0
        with ThreadPoolExecutor(max_workers=self.jobs) as ex:
            futures = {ex.submit(self._check_one, p): p for p in targets}
            for fut in as_completed(futures):
                p, ok, err = fut.result()
                checked += 1
                if not ok:
                    corrupt.append((p, err))
                else:
                    newly_clean.append(p)
        self.ui.say(f"  checked {checked} file(s): {len(corrupt)} corrupt")
        if newly_clean and not self.args.dry_run:
            with ThreadPoolExecutor(max_workers=self.jobs) as ex:
                clean_cache.update(k for k in ex.map(track_key, newly_clean) if k)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(clean_cache)))
            os.replace(tmp, cache_path)
        for p, err in sorted(corrupt):
            rel = p.relative_to(self.staging)
            # Strippable tag wrappers (leading ID3v2 / trailing ID3v1/APE) are a
            # common cause of FLAC "corruption" — repair silently before asking.
            if p.suffix.lower() == ".flac" and not self.args.dry_run and repair_flac(p):
                self.ui.say(f"  auto-repaired (illegal tag wrapping): {rel}")
                self.bump("flac_repaired")
                continue
            key = track_key(p)
            if key and self.store.track(key).get("corrupt") == "ignore":
                self.ui.say(f"  (previously ignored) {rel}")
                continue
            first_err = err.splitlines()[0] if err else "decode error"
            try:
                choice = self.ui.ask(
                    f"CORRUPT: {rel}\n    {first_err}",
                    {"q": f"quarantine to {QUARANTINE}/", "d": "delete",
                     "i": "ignore (remember: plays fine)",
                     "s": "skip (decide later, stays held)"}, "q", context=str(p))
            except Deferred:
                self.held.add(p)
                continue
            if choice == "s":
                self.held.add(p)
                continue
            if choice == "d":
                self.delete(p, "corrupt")
            elif choice == "q":
                qdir = self.staging / QUARANTINE / rel.parent
                if not self.args.dry_run:
                    qdir.mkdir(parents=True, exist_ok=True)
                    qdst = qdir / p.name
                    n = 1
                    while qdst.exists():  # never overwrite an earlier quarantine
                        qdst = qdir / f"{p.stem} ({n}){p.suffix}"
                        n += 1
                    shutil.move(str(p), qdst)
                self.ui.say(f"  quarantined: {rel}")
                self.bump("quarantined")
            elif choice == "i" and key:
                self.store.set_track(key, corrupt="ignore")

    @staticmethod
    def _check_one(p: Path) -> tuple[Path, bool, str]:
        if p.suffix.lower() == ".flac":
            r = subprocess.run(["flac", "--test", "--silent", str(p)],
                               capture_output=True, text=True)
            return p, r.returncode == 0, (r.stderr or "").strip()
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p),
                            "-map", "0:a", "-f", "null", "-"],
                           capture_output=True, text=True)
        err = (r.stderr or "").strip()
        return p, not err and r.returncode == 0, err

    # --- phase 4: group ---

    def phase_group(self) -> list[Unit]:
        self.ui.head("Phase 4/8 · Group into album units")
        # un-nesting can expose further nesting (doubly-nested units move along
        # with their parent), so iterate until a pass moves nothing
        for _ in range(8):
            units = discover_units(self.staging)
            if not self._relocate_nested_units(units):
                break
        units = discover_units(self.staging)
        if not units:
            self.ui.say("  staging is empty")
            return []
        self.ui.say(f"  {len(units)} unit(s); computing content keys...")
        all_files = [(u, f) for u in units for f in u.files]
        with ThreadPoolExecutor(max_workers=self.jobs) as ex:
            futures = {ex.submit(track_key, f): (u, f) for u, f in all_files}
            for fut in as_completed(futures):
                u, f = futures[fut]
                u.keys[f] = fut.result()
        for u in units:
            rel = u.dir.relative_to(self.staging) if u.dir != self.staging else Path("<staging root>")
            tag = " (loose files)" if u.loose else ""
            self.ui.say(f"    {rel}{tag}: {len(u.files)} track(s)")
        return units

    def _relocate_nested_units(self, units: list[Unit]) -> bool:
        """A unit physically inside another unit's dir (a NAMED disc folder like
        'CD2 - Relapse Refill', which is its own release) confuses beets: its
        multi-disc heuristics re-merge the subfolder into the parent's import.
        Move nested units up to the staging root so physical layout matches the
        logical grouping. Returns True if anything moved."""
        moved = False
        dirs = [u.dir for u in units if not u.loose]
        for unit in units:
            if unit.loose or not unit.dir.exists():
                continue  # may have vanished when an ancestor unit moved
            if not any(o != unit.dir and o in unit.dir.parents for o in dirs):
                continue
            target = self.staging / unit.dir.name
            if target.exists() or any(d.name == unit.dir.name and d != unit.dir
                                      for d in dirs):
                target = self.staging / f"{unit.dir.parent.name} - {unit.dir.name}"
            n = 1
            while target.exists():
                target = self.staging / f"{unit.dir.parent.name} - {unit.dir.name} ({n})"
                n += 1
            if self.args.dry_run:
                self.ui.say(f"  would un-nest: {unit.dir.relative_to(self.staging)}"
                            f" -> {target.name}")
                continue
            old = unit.dir
            shutil.move(str(old), target)
            # held entries are keyed by path; keep them pointing at the files
            for h in [h for h in self.held if old in h.parents]:
                self.held.discard(h)
                self.held.add(target / h.relative_to(old))
            self.ui.say(f"  un-nested: {old.relative_to(self.staging)} -> {target.name}")
            moved = True
        return moved

    # --- phase 5+6: replay & quiet import ---

    def phase_import(self, units: list[Unit]) -> tuple[list[Unit], dict]:
        self.ui.head("Phase 5/8 · Import (replays + MusicBrainz quiet pass)")
        leftovers: list[Unit] = []
        fuzzy_defaults: dict[str, dict] = {}
        for unit in units:
            rel = unit.dir.relative_to(self.staging) if unit.dir != self.staging else Path("<staging root>")
            held_here = [f for f in unit.files if f in self.held]
            if held_here:
                self.ui.say(f"  ⏸ held back ({len(held_here)} unresolved file(s)): {rel}")
                self.bump("held")
                continue
            akey = unit.key
            stored = self.store.album(akey) if akey else {}
            lrc_map = self._collect_lrc_map(unit)

            if stored.get("action") == "asis":
                self.ui.say(f"  ↻ replaying manual placement: {rel}")
                if not self.args.dry_run:
                    self._manual_place(unit, stored["artist"], stored.get("album", ""),
                                       stored.get("year", ""), remember=False,
                                       replay=True)
                self.bump("replayed")
                continue

            if unit.loose:
                leftovers.append(unit)
                continue

            search_id = stored.get("mbid") if stored.get("action") == "mbid" else None
            if search_id:
                self.ui.say(f"  ↻ replaying MusicBrainz match: {rel}")
                self.bump("replayed")
                if not self.args.dry_run and self._dedupe_against_library(unit, search_id):
                    continue
            elif akey and self.store.failed_recently(akey):
                # a quiet match against the same content failed within 72h:
                # don't burn MusicBrainz queries repeating it
                self.ui.say(f"  ⏭ recently unmatched, straight to resolution: {rel}")
                leftovers.append(unit)
                fz = self.store.fuzzy_album(list(filter(None, unit.keys.values())))
                if fz:
                    fuzzy_defaults[akey] = fz[1]
                continue
            else:
                self.ui.say(f"  → quiet import: {rel}")

            if self.args.dry_run:
                continue
            # beets prints $added at whole-second precision; align the marker
            t0 = datetime.now().replace(microsecond=0)
            proc = self.runner.quiet_import(unit.dir, search_id=search_id)
            if proc.returncode != 0:
                self.ui.say(f"      ERROR beets import failed (rc={proc.returncode}) "
                            f"— unit left in staging")
                for line in (proc.stderr or "").strip().splitlines()[-3:]:
                    self.ui.say(f"        {line}")
                self.bump("beets_errors")
                # albums beets created before failing must not be absorbed by
                # the next unit's same-second _record_import window
                self.seen_album_ids.update(
                    a["id"] for a in self.runner.albums_added_since(t0))
                continue
            if unit.audio_left():
                # record any PARTIAL import now: attribution is by content key,
                # and leaving it unrecorded would let the next unit absorb it
                # through the same-second window
                partial = self._record_import(unit, t0)
                if partial:
                    self._attach_lrc(partial, lrc_map)
                fz = self.store.fuzzy_album(list(filter(None, unit.keys.values())))
                if fz and akey:
                    fuzzy_defaults[akey] = fz[1]
                leftovers.append(unit)
                if akey and not search_id:
                    self.store.record_failed_attempt(akey)
                if partial:
                    self.ui.say(f"      partially imported — remaining files "
                                f"queued for resolution")
                else:
                    self.ui.say(f"      no confident match — queued for resolution")
                if not read_tags(unit.files[0]):
                    self.ui.say("      (untagged files: acoustic fingerprinting may be "
                                "rate-limited — [i] then 'enter Id' with a MusicBrainz "
                                "URL works best)")
            else:
                added = self._record_import(unit, t0)
                if added:
                    self._attach_lrc(added, lrc_map)
                    self._cleanup_unit_dir(unit, salvage_dir=Path(added[0]["path"]))
                else:
                    self.ui.say("      WARNING import succeeded but the album could not "
                                "be identified in the beets db — companions kept in "
                                "staging for safety")
        return leftovers, fuzzy_defaults

    def _dedupe_against_library(self, unit: Unit, mbid: str) -> bool:
        """A re-staged album whose MusicBrainz match is already in the library:
        remove content-identical staged duplicates (beets quiet mode would just
        skip them as duplicates, stranding them in staging forever). Returns
        True if the whole unit was resolved this way."""
        existing = self.runner.album_by_mbid(mbid)
        if not existing:
            return False
        items = self.runner.items_for_album(existing["id"])
        # discard None keys: an unreadable file must never "match" another
        key_to_lib = {k: Path(it["path"]) for it in items
                      if Path(it["path"]).exists()
                      if (k := track_key(Path(it["path"]))) is not None}
        dups = [(f, key_to_lib[k]) for f in unit.audio_left()
                if (k := unit.keys.get(f) or track_key(f)) is not None
                and k in key_to_lib]
        for f, lib_file in dups:
            self._adopt_lrc(f, lib_file)
            f.unlink()
        if dups:
            self.ui.say(f"      {len(dups)} file(s) already in the library "
                        f"(content-identical) — staged duplicates removed")
            self.bump("replay_dedup", len(dups))
        if not unit.audio_left():
            self._cleanup_unit_dir(unit, salvage_dir=Path(existing["path"]))
            return True
        return False

    @staticmethod
    def _adopt_lrc(staged_audio: Path, lib_audio: Path):
        """The staged audio is a duplicate about to be removed — but its .lrc
        sidecar may be new. Give it to the library copy if that copy has none;
        otherwise leave it in the unit dir for the salvage sweep."""
        lrc = staged_audio.with_suffix(".lrc")
        if lrc.exists():
            dst = lib_audio.with_suffix(".lrc")
            if not dst.exists():
                shutil.move(str(lrc), dst)

    def _record_import(self, unit: Unit, since: datetime) -> list[dict]:
        """Record every album beets created from this unit. One unit can yield
        several albums (e.g. beets' 'Group albums' in an interactive session),
        so each album's decision entry derives its track keys from the files it
        actually received — content keys are path-independent, so a future
        re-stage of just that album finds its own correct entry."""
        added = self.runner.albums_added_since(since)
        # same-second window can over-include a previous unit's albums
        added = [a for a in added if a["id"] not in self.seen_album_ids]
        self.seen_album_ids.update(a["id"] for a in added)
        for album in added:
            items = self.runner.items_for_album(album["id"])
            keys = list(filter(None, (track_key(Path(it["path"])) for it in items
                                      if Path(it["path"]).exists())))
            if keys and album["mb_albumid"]:
                self.store.set_album(
                    album_key(keys), action="mbid", mbid=album["mb_albumid"],
                    track_keys=keys, artist=album["albumartist"],
                    album=album["album"], year=album["year"])
            elif keys:
                # interactive "Use as-is": no MusicBrainz id — replay as a
                # manual placement so re-staging never asks again
                self.store.set_album(
                    album_key(keys), action="asis", track_keys=keys,
                    artist=album["albumartist"], album=album["album"],
                    year=album["year"])
            self.imported_albums.append(album)
            self.ui.say(f"      imported: {album['albumartist']} — [{album['year']}] {album['album']}")
            self.bump("imported")
        return added

    # --- companions ---

    def _collect_lrc_map(self, unit: Unit) -> list[dict]:
        out = []
        for f in unit.files:
            lrc = f.with_suffix(".lrc")
            if lrc.exists():
                tags = read_tags(f)
                out.append({
                    "lrc": lrc,
                    "disc": parse_intlike(tags.get("discnumber", "")) or 1,
                    "track": parse_intlike(tags.get("tracknumber", "")),
                    "stem": f.stem,
                })
        return out

    def _attach_lrc(self, albums: list[dict], lrc_map: list[dict]):
        if not lrc_map or not albums:
            return
        items = []
        for album in albums:
            items.extend(self.runner.items_for_album(album["id"]))
        attached = 0
        for entry in lrc_map:
            candidates = [it for it in items
                          if it["track"] == entry["track"]
                          and max(it["disc"], 1) == entry["disc"]]
            if not candidates:
                # missing track tags (track=0): match by title in the old stem
                candidates = [it for it in items
                              if it["title"] and it["title"].lower() in entry["stem"].lower()]
            if len(candidates) > 1:
                # several albums from one session: disambiguate by title
                by_title = [it for it in candidates
                            if it["title"] and it["title"].lower() in entry["stem"].lower()]
                candidates = by_title or candidates
            if candidates and entry["lrc"].exists():
                dst = Path(candidates[0]["path"]).with_suffix(".lrc")
                if dst.exists():
                    continue  # never destroy an already-attached .lrc
                shutil.copy2(entry["lrc"], dst)
                entry["lrc"].unlink()  # leftovers in the unit dir = unattached
                attached += 1
        if attached:
            self.ui.say(f"      lyrics: {attached} .lrc file(s) attached")
            self.bump("lrc_attached", attached)

    def _cleanup_unit_dir(self, unit: Unit, salvage_dir: Path | None = None):
        """After a successful import/placement, the unit dir holds only
        leftovers. Remove it — but first salvage anything a librarian would
        regret losing (unmatched .lrc, scans/artwork, booklets, cue sheets)
        into the album's library directory."""
        if unit.dir == self.staging or self.args.dry_run:
            return
        if unit.audio_left():
            return
        salvageable = [f for f in sorted(unit.dir.rglob("*"))
                       if f.is_file() and f.suffix.lower() in (COMPANION | IMAGES
                                                               | KEEP_UNTIL_RESOLVED)]
        if salvageable:
            if not (salvage_dir and salvage_dir.is_dir()):
                # no valid destination: refuse to destroy companions
                self.ui.say(f"      companions kept in staging (no album dir to "
                            f"salvage into): {unit.dir.relative_to(self.staging)}")
                return
            for f in salvageable:
                shutil.move(str(f), unique_path(salvage_dir / f.name))
        shutil.rmtree(unit.dir, ignore_errors=True)

    # --- phase 7: interactive resolution ---

    def phase_resolve(self, leftovers: list[Unit], fuzzy_defaults: dict):
        self.ui.head("Phase 6/8 · Resolve unmatched units")
        if not leftovers:
            self.ui.say("  nothing left to resolve")
            return
        for unit in leftovers:
            rel = unit.dir.relative_to(self.staging) if unit.dir != self.staging else Path("<staging root>")
            files = unit.audio_left()
            if not files:
                continue
            if any(f in self.held for f in unit.files):
                self.ui.say(f"\n  ▸ {rel} — held back until its file problems are resolved")
                continue
            tags = read_tags(files[0])
            hint = " / ".join(filter(None, [tags.get("albumartist") or tags.get("artist", ""),
                                            tags.get("album", "")]))
            fz = fuzzy_defaults.get(unit.key or "", None)
            self.ui.say(f"\n  ▸ {rel} — {len(files)} file(s)" + (f"  [{hint}]" if hint else ""))
            if fz:
                self.ui.say(f"    looks like previously decided: {fz.get('artist', '')} — {fz.get('album', '')}")
            mp3s = [f for f in files if f.suffix.lower() == ".mp3"]
            if mp3s:
                unknown = [f for f in mp3s
                           if not self.store.track(unit.keys.get(f) or "").get("allowed_mp3")]
                if unknown:
                    self.ui.say(f"    note: contains {len(mp3s)} MP3 file(s) (FLAC-first library)")
            if unit.loose:
                # beets on the staging root would import every other unit too
                menu = {"m": "manual placement", "s": "skip (leave in staging)",
                        "d": "delete"}
                default = "m" if fz else "s"
            else:
                menu = {"i": "interactive beets import", "m": "manual placement",
                        "s": "skip (leave in staging)", "d": "delete"}
                default = "m" if fz else "i"
            try:
                choice = self.ui.ask("    how should this be handled?", menu,
                                     default, context=str(unit.dir))
            except Deferred:
                continue
            if choice == "s":
                self.bump("skipped")
                continue
            if choice == "d":
                try:
                    sure = self.ui.ask(f"    really delete {rel}?",
                                       {"y": "yes", "n": "no"}, "n", context=str(unit.dir))
                except Deferred:
                    continue
                if sure == "y" and not self.args.dry_run:
                    if unit.loose:
                        for f in files:
                            f.unlink()
                    else:
                        shutil.rmtree(unit.dir)
                    self.bump("deleted_units")
                continue
            if choice == "i":
                lrc_map = self._collect_lrc_map(unit)
                t0 = datetime.now().replace(microsecond=0)
                self.runner.interactive_import(unit.dir)
                added = self._record_import(unit, t0)
                if not unit.audio_left():
                    if added:
                        self._attach_lrc(added, lrc_map)
                        self._cleanup_unit_dir(unit, salvage_dir=Path(added[0]["path"]))
                    else:
                        # singleton/'as Tracks' import: audio moved but no album
                        # row exists — never rmtree blind; companions stay put
                        self.ui.say("      imported without an album entry "
                                    "('as Tracks'?) — companions kept in staging")
                    for f in unit.files:
                        if f.suffix.lower() == ".mp3":
                            k = unit.keys.get(f)
                            if k:
                                self.store.set_track(k, allowed_mp3=True)
                else:
                    self._attach_lrc(added, lrc_map)
                    self.ui.say("      still unresolved — staying in staging")
                    self.bump("skipped")
                continue
            # manual placement
            default_artist = (fz or {}).get("artist") or tags.get("albumartist") \
                or tags.get("artist") or (unit.dir.name if not unit.loose else "")
            canon = self.store.canonical_artist(default_artist)
            if canon:
                default_artist = canon
            else:
                suspicion = detect(default_artist, self.known_artists(),
                                   self.confirmed_artists())
                if suspicion and not suspicion.default_keep:
                    default_artist = suspicion.suggestion
            default_album = (fz or {}).get("album") or tags.get("album") \
                or (unit.dir.name if not unit.loose else "")
            default_year = ((fz or {}).get("year") or tags.get("originaldate")
                            or tags.get("date") or "")[:4]
            m = re.search(r"(19|20)\d\d", unit.dir.name)
            if not default_year and m:
                default_year = m.group(0)
            try:
                artist = self.ui.ask_text("    artist", default_artist, context=str(unit.dir))
                albumn = self.ui.ask_text("    album", default_album, context=str(unit.dir))
                year = self.ui.ask_text("    year (blank = none)", default_year,
                                        context=str(unit.dir))
            except Deferred:
                continue
            self._manual_place(unit, artist, albumn, year, remember=True)
            for f in unit.files:
                if f.suffix.lower() == ".mp3":
                    k = unit.keys.get(f)
                    if k:
                        self.store.set_track(k, allowed_mp3=True)

    def _manual_place(self, unit: Unit, artist: str, album: str, year: str,
                      remember: bool, replay: bool = False):
        prefix = f"[{year}] " if year else ""
        target = self.library / sanitize(artist) / f"{prefix}{sanitize(album)}"
        files = unit.audio_left()
        if self.args.dry_run:
            self.ui.say(f"    would move {len(files)} file(s) -> {target.relative_to(self.library)}")
            return
        if target.exists() and any(f.suffix.lower() in ALL_AUDIO for f in target.iterdir()):
            if replay:
                # Re-staged copy of an album that is already in the library:
                # prove duplication by content key and discard the duplicates —
                # the whole point of replay is to be hands-free.
                key_to_lib = {k: f for f in target.iterdir()
                              if f.is_file() and f.suffix.lower() in ALL_AUDIO
                              if (k := track_key(f)) is not None}
                dup = [(f, key_to_lib[k]) for f in files
                       if (k := unit.keys.get(f) or track_key(f)) is not None
                       and k in key_to_lib]
                for f, lib_file in dup:
                    self._adopt_lrc(f, lib_file)
                    f.unlink()
                if dup:
                    self.ui.say(f"    {len(dup)} file(s) already in the library "
                                f"(content-identical) — staged duplicates removed")
                    self.bump("replay_dedup", len(dup))
                files = unit.audio_left()
                if not files:
                    self._cleanup_unit_dir(unit, salvage_dir=target)
                    return
                # remaining files are new tracks of a known album: merge quietly
            else:
                try:
                    merge = self.ui.ask(
                        f"    target '{target.relative_to(self.library)}' already has audio. Merge into it?",
                        {"y": "merge", "n": "cancel (leave in staging)"}, "n",
                        context=str(unit.dir))
                except Deferred:
                    return
                if merge != "y":
                    self.bump("skipped")
                    return
        target.mkdir(parents=True, exist_ok=True)
        # read every file's tags ONCE, before anything moves
        all_tags = {f: read_tags(f) for f in sorted(files)}
        discs = {parse_intlike(t.get("discnumber", "")) or 1 for t in all_tags.values()}
        multidisc = len(discs) > 1
        # keys of the files THIS placement moves — a partial beets import may
        # have taken other unit files already, and the remembered decision must
        # not claim those (computed pre-move; keys are content-based)
        placed_keys = [k for f in all_tags
                       if (k := unit.keys.get(f) or track_key(f)) is not None]
        moved = 0
        for f, tags in all_tags.items():
            track = parse_intlike(tags.get("tracknumber", ""))
            disc = parse_intlike(tags.get("discnumber", "")) or 1
            title = tags.get("title") or f.stem
            if track:
                num = f"{disc}{track:02d} - " if multidisc else f"{track:02d} - "
            else:
                num = ""
            dst = unique_path(target / f"{num}{sanitize(title)}{f.suffix.lower()}")
            lrc = f.with_suffix(".lrc")
            shutil.move(str(f), dst)
            if lrc.exists():
                shutil.move(str(lrc), unique_path(dst.with_suffix(".lrc")))
            self._fill_missing_tags(dst, artist, album, year, title, track, disc,
                                    multidisc)
            moved += 1
        # images, booklets, cue sheets, unmatched lyrics — salvage recursively
        self._cleanup_unit_dir(unit, salvage_dir=target)
        self.ui.say(f"    placed {moved} file(s) -> {target.relative_to(self.library)}")
        self.bump("manual_placed")
        if remember and placed_keys:
            self.store.set_album(album_key(placed_keys), action="asis",
                                 track_keys=placed_keys,
                                 artist=artist, album=album, year=year)

    @staticmethod
    def _fill_missing_tags(path: Path, artist: str, album: str, year: str,
                           title: str, track: int, disc: int, multidisc: bool):
        """Manual placements bypass beets, so the user's typed answers would
        otherwise live only in the directory name. Write them into the files —
        but never overwrite an existing tag."""
        try:
            import mutagen
            audio = mutagen.File(path, easy=True)
            if audio is None:
                return
            if audio.tags is None:
                audio.add_tags()
            def put(key, value):
                if value and not audio.tags.get(key):
                    audio.tags[key] = [str(value)]
            put("albumartist", artist)
            put("artist", artist)
            put("album", album)
            put("date", year)
            put("title", title)
            if track:
                put("tracknumber", track)
            if multidisc and disc:
                put("discnumber", disc)
            audio.save()
        except Exception:
            pass  # tags are a courtesy; never fail a placement over them

    # --- phase 8: albumartist normalization ---

    def phase_normalize(self):
        self.ui.head("Phase 7/8 · Albumartist normalization (device sanity)")
        # Sweep the whole beets db, not just this run's imports: a normalization
        # deferred in --auto mode must resurface on the next interactive run.
        albums = [] if self.args.dry_run else self.runner.all_albums()
        if not albums:
            self.ui.say("  no albums in beets db")
            return
        known = self.known_artists()
        confirmed = self.confirmed_artists()
        for album in albums:
            aa = album["albumartist"]
            canon = self.store.canonical_artist(aa)
            if canon is not None:
                if canon != aa:
                    self._apply_albumartist(album, canon)
                continue
            suspicion = detect(aa, known, confirmed)
            if not suspicion:
                continue
            # Auto-primary policy: apply silently when the suggestion is
            # corroborated by evidence — the credit's lead is the tracks'
            # COMPOSER (classical: file under the composer's sort name), or a
            # split part is an artist already in the library. Uncorroborated
            # credits (e.g. 'Simon & Garfunkel') still ask, once ever.
            corroborated = self._corroborated_primary(album, suspicion, known)
            if corroborated is not None:
                auto_value, reason = corroborated
                if self._apply_albumartist(album, auto_value):
                    self.store.set_canonical_artist(aa, auto_value)
                    self.ui.say(f"    (auto: {reason})")
                continue
            # suggestion first (library-spelled), then the other split parts
            options = [suspicion.suggestion] + [
                p for p in suspicion.parts if p.lower() != suspicion.suggestion.lower()]
            options = options[:4]
            choices = {str(i): f"use '{part}'" for i, part in enumerate(options, 1)}
            choices["k"] = "keep as-is"
            choices["c"] = "custom"
            default = "k" if suspicion.default_keep else "1"
            try:
                choice = self.ui.ask(
                    f"  '{aa}' ({album['album']}) looks like multiple artists "
                    f"(separator: {suspicion.separator}).\n"
                    f"  Devices will list it as a separate artist. Canonical primary?",
                    choices, default, context=aa)
            except Deferred:
                continue
            if choice == "k":
                self.store.set_canonical_artist(aa, aa)
                continue
            if choice == "c":
                try:
                    value = self.ui.ask_text("  canonical artist", suspicion.suggestion,
                                             context=aa)
                except Deferred:
                    continue
            else:
                value = options[int(choice) - 1]
            # only remember the mapping once it has actually been applied
            if self._apply_albumartist(album, value):
                self.store.set_canonical_artist(aa, value)

    def _corroborated_primary(self, album: dict, suspicion, known: set[str]) \
            -> tuple[str, str] | None:
        """Evidence that makes a primary-artist split safe to auto-apply.

        Classical: the credit's lead segment is the tracks' COMPOSER tag —
        canonicalize to the composer's sort name ('Bach, Johann Sebastian').
        Known artist: a split part already exists in the library."""
        if suspicion.default_keep:
            return None
        composers, aa_sort = self.runner.album_composer_info(album["id"])
        lead = suspicion.parts[0]
        for comp in composers:
            if fold(comp) == fold(lead):
                # first segment of the sort credit = composer sort name
                sort_lead = re.split(r"\s*;\s*", aa_sort)[0].strip() if aa_sort else ""
                value = sort_lead or lead
                return value, f"credit lead '{lead}' is the composer tag"
        known_folded = {fold(a) for a in known}
        for part in suspicion.parts:
            if fold(part) in known_folded:
                return suspicion.suggestion, f"'{suspicion.suggestion}' already in library"
        return None

    def _apply_albumartist(self, album: dict, value: str) -> bool:
        if self.args.dry_run:
            self.ui.say(f"  would set albumartist: '{album['albumartist']}' -> '{value}'")
            return False
        old_dir = Path(album["path"])
        proc = self.runner.set_albumartist(album["id"], value)
        if proc.returncode != 0:
            self.ui.say(f"  ERROR beets modify failed for '{album['album']}' "
                        f"(rc={proc.returncode}) — not recording the mapping")
            for line in (proc.stderr or "").strip().splitlines()[-2:]:
                self.ui.say(f"    {line}")
            return False
        # beets moved the audio it tracks; companions (.lrc, salvaged art,
        # booklets) it knows nothing about would be stranded in the old dir
        fields = self.runner.album_fields(album["id"])
        new_dir = Path(fields["path"]) if fields else None
        if new_dir and new_dir != old_dir and old_dir.is_dir() and new_dir.is_dir():
            for f in sorted(old_dir.rglob("*")):
                if f.is_file():
                    shutil.move(str(f), unique_path(new_dir / f.name))
            # emptied subdirs bottom-up, then the album dir, then the artist dir
            for d in sorted((p for p in old_dir.rglob("*") if p.is_dir()),
                            reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
            for d in (old_dir, old_dir.parent):
                try:
                    d.rmdir()
                except OSError:
                    break
        if new_dir:
            album["path"] = str(new_dir)
        self.ui.say(f"  albumartist: '{album['albumartist']}' -> '{value}' "
                    f"({album['album']}) — files re-pathed, track artists preserved")
        self.bump("normalized")
        return True

    # --- phase 9: post-check + sync ---

    def phase_post(self):
        self.ui.head("Phase 8/8 · Post-checks")
        bad = []
        for album in self.imported_albums:
            # normalization may have re-pathed the album since import
            fields = self.runner.album_fields(album["id"])
            apath = Path(fields["path"]) if fields else Path(album["path"])
            if not apath.exists():
                continue
            for f in apath.rglob("*"):
                if f.suffix.lower() == ".flac" and sniff(f) not in ("flac",):
                    bad.append(f)
        if bad:
            for f in bad:
                self.ui.say(f"  WARNING bad flac after import: {f}")
        else:
            self.ui.say(f"  verified {len(self.imported_albums)} imported album(s)")

    def phase_sync(self):
        if not self.mp3_target or self.args.dry_run:
            return
        if not (self.args.sync or self.auto_sync):
            return
        self.ui.head("MP3 mirror sync")
        cmd = [sys.executable, str(REPO_ROOT / "flac_mp3_sync" / "sync.py"),
               str(self.library), str(Path(self.mp3_target).expanduser()),
               "--bitrate", self.bitrate, "--jobs", str(self.jobs)]
        subprocess.run(cmd)

    # --- driver ---

    def run(self) -> int:
        if not self.staging.is_dir():
            print(f"Error: staging '{self.staging}' is not a directory")
            return 1
        mode = "DRY RUN" if self.args.dry_run else ("AUTO" if self.args.auto else "interactive")
        self.ui.say(f"intake · library={self.library} · staging={self.staging} · mode={mode}")

        self._acquire_lock()
        try:
            self.phase_strip()
            self.phase_validate()
            self.phase_corruption()
            units = self.phase_group()
            if units:
                leftovers, fuzzy = self.phase_import(units)
                self.phase_resolve(leftovers, fuzzy)
            # normalization sweeps the whole beets db: deferred questions from
            # earlier --auto runs must resurface even when staging is empty
            self.phase_normalize()
            self.phase_post()
            self.phase_sync()
        finally:
            self._release_lock()

        self.ui.head("Summary")
        for k in sorted(self.stats):
            self.ui.say(f"  {k:16s} {self.stats[k]}")
        if self.store.mutations:
            self.ui.say(f"  {'remembered':16s} {self.store.mutations} decision(s) saved to "
                        f"{self.store.path}")
        exit_code = 0
        if self.ui.deferred:
            self.ui.say(f"\n  deferred ({len(self.ui.deferred)}) — run interactively to resolve:")
            for d in dict.fromkeys(self.ui.deferred):
                self.ui.say(f"    - {d}")
            exit_code = 2
        leftover = [f for f in self.staging.rglob("*")
                    if f.is_file() and f.suffix.lower() in ALL_AUDIO
                    and QUARANTINE not in f.relative_to(self.staging).parts]
        if leftover:
            self.ui.say(f"\n  staging still holds {len(leftover)} audio file(s) "
                        f"(skipped/held) — they will be revisited next run")
            exit_code = 2
        companions = [f for f in self.staging.rglob("*")
                      if f.is_file() and f.suffix.lower() in (COMPANION | IMAGES
                                                              | KEEP_UNTIL_RESOLVED)
                      and QUARANTINE not in f.relative_to(self.staging).parts]
        if companions and not leftover:
            self.ui.say(f"\n  staging holds {len(companions)} companion file(s) "
                        f"with no audio (lyrics/art/booklets) — review manually:")
            for f in companions[:10]:
                self.ui.say(f"    - {f.relative_to(self.staging)}")
        return exit_code

    def _acquire_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    f.write(str(os.getpid()))
                return
            except FileExistsError:
                try:
                    pid = int(self.lock_path.read_text().strip())
                except (ValueError, OSError):
                    pid = None
                if pid is not None:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except ProcessLookupError:
                        alive = False  # genuinely stale
                    except PermissionError:
                        alive = True   # exists but isn't ours: definitely held
                    if alive:
                        raise SystemExit(
                            f"another intake run (pid {pid}) holds {self.lock_path}")
                if attempt == 0:
                    # second O_EXCL attempt closes the unlink/create race window
                    self.lock_path.unlink(missing_ok=True)
        raise SystemExit(f"could not acquire {self.lock_path}")

    def _release_lock(self):
        self.lock_path.unlink(missing_ok=True)


# ----------------------------------------------------------------------- cli

def load_config(path: Path | None) -> dict:
    candidates = [path] if path else [Path("intake.toml"), REPO_ROOT / "intake.toml"]
    for c in candidates:
        if c and c.is_file():
            with open(c, "rb") as f:
                return tomllib.load(f)
    raise SystemExit(
        "No intake.toml found. Copy intake/config.example.toml to intake.toml "
        "and set your paths (or pass --config).")


def main():
    parser = argparse.ArgumentParser(description="Process everything in _Staging.")
    parser.add_argument("--config", type=Path, help="path to intake.toml")
    parser.add_argument("--auto", action="store_true",
                        help="non-interactive: apply stored decisions, defer questions")
    parser.add_argument("--dry-run", action="store_true", help="report only")
    parser.add_argument("--sync", action="store_true", help="run MP3 mirror sync at the end")
    args = parser.parse_args()

    cfg = load_config(args.config)
    pipeline = Pipeline(cfg, args)
    sys.exit(pipeline.run())


if __name__ == "__main__":
    main()
