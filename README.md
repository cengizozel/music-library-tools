# music-library-tools

Personal scripts for managing and maintaining my FLAC music library.

## Tools

### 1. FLAC Validator
Scans the library and flags any file that is not a FLAC. Catches wrong formats that slipped in (MP3, AAC, WAV, etc.).

### 2. Corruption Checker
Verifies the audio data of every FLAC file is intact, not just that the extension is correct.

### 3. FLAC-to-MP3 Sync
One-way sync from the FLAC library to a separate MP3 directory (e.g. a microSD card).
- Converts FLAC → MP3 for new files
- Skips files that haven't changed
- Replaces MP3s where the source FLAC was modified or replaced
- Removes MP3s where the source FLAC was deleted
- The MP3 directory is always a derived copy — never edited directly

### 4. Library Organizer
Cleans up and restructures the FLAC library.
- Looks up correct artist/album metadata via MusicBrainz
- Renames files and folders to a consistent format
- Splits by release type: album, EP, single, compilation
- Ensures cover art is present and embedded

## Library Layout

```
Artist/
  [Year] Album/
    01 - Track Title.flac

_Compilations/
  [Year] Album/
    01 - Artist - Track Title.flac

_Singles/
  Artist/
    [Year] Track Title.flac

_EPs/
  Artist/
    [Year] Album [EP]/
      01 - Track Title.flac
```

## Philosophy

- Fix the original FLAC library first, everything else derives from it
- No destructive changes without a dry-run first
- Semi-automatic by default — confirm before bulk renames
