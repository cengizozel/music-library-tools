# music-library-tools

Personal scripts for managing and maintaining my FLAC music library.

## Tools

### 1. FLAC Validator
Scans the library and flags any file that is not a valid FLAC — checks both file extension and actual file header (magic bytes).

```bash
python3 flac_validator/validate.py ~/Music/Library
```

To exempt directories that intentionally contain non-FLAC files (old demos, YouTube rips, etc.) from the check, create a `.flac_exempt` file in the library root — one directory name per line:

```
# directories exempt from the FLAC requirement
_NonFLAC
_Demos
_YouTubeRips
```

### 2. Corruption Checker
Verifies the audio data of every FLAC and MP3 file. FLACs are checked using `flac --test` (full decode + MD5 verification). MP3s are checked using `ffmpeg -v error`.

```bash
python3 corruption_checker/check.py ~/Music/Library
python3 corruption_checker/check.py ~/Music/Library --jobs 8
```

Requires: `brew install flac` (for FLACs) and/or `brew install ffmpeg` (for MP3s) — only whichever is present in the library.

### 3. FLAC-to-MP3 Sync
One-way sync from the FLAC library to a separate MP3 directory (e.g. a microSD card). Converts new files, skips unchanged ones, reconverts modified ones, and removes MP3s whose source FLAC was deleted.

```bash
# preview
python3 flac_mp3_sync/sync.py ~/Music/Library ~/Music/MP3 --dry-run

# run
python3 flac_mp3_sync/sync.py ~/Music/Library ~/Music/MP3
python3 flac_mp3_sync/sync.py ~/Music/Library ~/Music/MP3 --bitrate 320k --jobs 8
```

Requires: `brew install ffmpeg`

The MP3 directory is always a derived copy — never edit it directly.

### 4. Library Organizer
Restructures a messy library (FLAC and MP3) into a consistent layout based on tags. Detects release type, renames files, moves albums into the right folders, and optionally downloads missing cover art from Cover Art Archive.

```bash
pip install mutagen requests

# preview
python3 library_organizer/organize.py ~/Music/Library

# apply
python3 library_organizer/organize.py ~/Music/Library --apply

# apply + fetch missing cover art
python3 library_organizer/organize.py ~/Music/Library --apply --fetch-art
```

Dry-run by default. Reports albums with missing tags before you commit to anything.

### 5. Strip Non-Audio
Removes all non-FLAC/MP3 files from an incoming directory before import — `.log`, `.m3u`, `.txt`, `.jpg`, `Thumbs.db`, etc. Also removes empty directories left behind.

```bash
# preview
python3 strip_non_audio/strip.py ~/Music/Incoming

# apply
python3 strip_non_audio/strip.py ~/Music/Incoming --apply
```

Dry-run by default.

## Library Layout

The folder structure is always `Artist/[Year] Project/Track.ext` regardless of release type (album, EP, single, compilation, soundtrack). Release type is stored in tags only — not in folder names. This ensures compatibility with Plex, Navidrome, Rockbox, and Neutron.

```
Artist/
  [Year] Album/
    01 - Track Title.flac

Artist/
  [Year] Album [EP]/        ← [EP] suffix is cosmetic only
    01 - Track Title.flac

Various Artists/
  [Year] Compilation/
    01 - Track Title.flac

Artist/
  [Year] Multi-Disc Album/
    101 - Track Title.flac  ← disc 1
    102 - Track Title.flac
    201 - Track Title.flac  ← disc 2
    202 - Track Title.flac
```

## Workflow

```
Anything messy (any folder structure, any tags)
  → Strip Non-Audio          — remove .log, .m3u, .txt, cover art, etc.
  → beet import -q           — auto-tag and move matched albums into library
                               unmatched albums stay in _Unmatched/
  → FLAC Validator           — flag any non-FLAC files
  → Corruption Checker       — verify audio integrity
  → FLAC-to-MP3 Sync         — push changes to MP3 mirror (microSD etc.)

To handle _Unmatched manually:
  → beet import              — interactive mode, pick matches one by one
```

The Library Organizer is a separate tool for re-organizing an already-imported library (e.g. after changing naming conventions). It is not part of the regular intake flow.

### Importing with beets

Beets identifies music via MusicBrainz (using existing tags or acoustic fingerprinting), writes corrected tags, and moves files into the library in the correct structure — all in one step. It accepts any messy input.

Before first use, open [beets/config.yaml](beets/config.yaml) and update these two lines:

```yaml
directory: /your/library/path        # ← set this to your library directory

fpcalc: /opt/homebrew/bin/fpcalc    # ← set this to the output of: which fpcalc
```

Acoustic fingerprinting requires `chromaprint` — no Python-only alternative exists:

```bash
# macOS
brew install chromaprint

# Linux (Debian/Ubuntu)
sudo apt install libchromaprint-tools
```

```bash
# normal intake — fully automatic, no prompts
venv/bin/beet -c beets/config.yaml import -q ~/Music/Library/_Unmatched

# manual review — interactive, for albums left in _Unmatched
venv/bin/beet -c beets/config.yaml import ~/Music/Library/_Unmatched

# re-sync tags for files already in the library using their MusicBrainz IDs
venv/bin/beet -c beets/config.yaml mbsync
```

Albums that cannot be matched automatically are routed to `_Unmatched/` inside the library. Run the manual review command to handle them one by one — beet will prompt you to pick a match, enter a MusicBrainz ID, or search by name.

## Philosophy

- Fix the original FLAC library first — everything else derives from it
- Dry-run before applying anything destructive
- The MP3 directory is never a source of truth
