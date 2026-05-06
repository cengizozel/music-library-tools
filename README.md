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
Incoming music (any state, any tags)
  → beet import              — fetch correct tags from MusicBrainz, embed cover art
  → FLAC Validator           — flag any non-FLAC files
  → Corruption Checker       — verify audio integrity
  → Library Organizer        — rename and restructure based on tags (dry-run first)
  → FLAC-to-MP3 Sync         — push changes to MP3 mirror (microSD etc.)
```

### Tagging with beets

Beets queries MusicBrainz for metadata and writes correct tags to your files. It uses acoustic fingerprinting for files with missing or wrong tags.

Acoustic fingerprinting requires `chromaprint` (`fpcalc` binary) — this is a native library with no Python-only alternative:

```bash
# macOS
brew install chromaprint

# Linux (Debian/Ubuntu)
sudo apt install libchromaprint-tools
```

```bash
# tag a folder of music (timid mode — confirms uncertain matches)
venv/bin/beet -c beets/config.yaml import ~/Music/Incoming

# re-sync tags for files that already have MusicBrainz IDs
venv/bin/beet -c beets/config.yaml mbsync
```

Beets is interactive — run it directly in your terminal. It will ask you to confirm matches it's unsure about.

Beets does **not** move or rename files — the Library Organizer handles that after tagging.

## Philosophy

- Fix the original FLAC library first — everything else derives from it
- Dry-run before applying anything destructive
- The MP3 directory is never a source of truth
