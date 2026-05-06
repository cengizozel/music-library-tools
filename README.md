# music-library-tools

Personal scripts for managing and maintaining my FLAC music library.

## Tools

### 1. FLAC Validator
Scans the library and flags any file that is not a valid FLAC — checks both file extension and actual file header (magic bytes).

```bash
python3 flac_validator/validate.py ~/Music/Library
```

### 2. Corruption Checker
Verifies the audio data of every FLAC file is intact using `flac --test`, which fully decodes each file and checks its MD5 checksum.

```bash
python3 corruption_checker/check.py ~/Music/Library
python3 corruption_checker/check.py ~/Music/Library --jobs 8
```

Requires: `brew install flac`

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
Restructures a messy FLAC library into a consistent layout based on tags. Detects release type, renames files, moves albums into the right folders, and optionally downloads missing cover art from Cover Art Archive.

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

```
Artist/
  [Year] Album/
    01 - Track Title.flac

_EPs/
  Artist/
    [Year] Album [EP]/
      01 - Track Title.flac

_Singles/
  Artist/
    [Year] Track Title.flac

_Compilations/
  [Year] Album/
    01 - Artist - Track Title.flac

_Soundtracks/
  [Year] Album/
    01 - Track Title.flac
```

## Workflow

```
Messy library
  → run Library Organizer (dry-run first, then --apply)
  → run FLAC Validator    (catch any non-FLAC files)
  → run Corruption Checker
  → run FLAC-to-MP3 Sync  (whenever you want to update the MP3 copy)
```

## Philosophy

- Fix the original FLAC library first — everything else derives from it
- Dry-run before applying anything destructive
- The MP3 directory is never a source of truth
