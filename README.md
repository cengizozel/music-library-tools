# music-library-tools

Personal scripts for managing and maintaining my FLAC music library.

The library is the single source of truth: new music lands in `_Staging/`, gets
quality-gated, identified against MusicBrainz, and filed into a fixed
`Artist/[Year] Album/` structure with clean tags — so tag-driven players
(Navidrome, Plex) and file-driven devices (Rockbox iPod, Neutron) all see the
same sane library. An MP3 mirror is derived from it for devices.

## One-time setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# acoustic fingerprinting (no root needed — static binary into the venv):
curl -sL https://github.com/acoustid/chromaprint/releases/download/v1.5.1/chromaprint-fpcalc-1.5.1-linux-x86_64.tar.gz \
  | tar xz --strip-components=1 -C venv/bin chromaprint-fpcalc-1.5.1-linux-x86_64/fpcalc

# system tools:
sudo pacman -S flac ffmpeg        # Arch
# brew install flac ffmpeg        # macOS

cp intake/config.example.toml intake.toml   # then edit paths
```

## The workflow: one command

Drop anything into `<library>/_Staging/` (any folder structure, any tags), then:

```bash
venv/bin/python intake/intake.py            # interactive
venv/bin/python intake/intake.py --auto     # cron-friendly: defers questions
venv/bin/python intake/intake.py --dry-run  # show what would happen
venv/bin/python intake/intake.py --sync     # also update the MP3 mirror
```

Phases, automatic unless a decision is genuinely yours to make:

1. **Strip** — junk files deleted (`.nfo .log .m3u .txt`...). Audio (any format),
   `.lrc` lyrics, cover images, and `.pdf`/`.cue` identification aids survive —
   the latter are cleaned up only once their album successfully lands.
2. **Validate** — magic-byte check of every file. Auto-fixes: FLAC data with an
   `.mp3` extension is renamed; FLACs with illegally prepended/appended ID3 tags
   are repaired losslessly. Real anomalies (MP3 data inside a `.flac`, zero-byte
   files, `.wav`/`.m4a` strays) become questions with sane defaults.
3. **Corruption check** — `flac --test` / ffmpeg decode, parallel. Corrupt files
   are quarantined to `_Staging/_Corrupt/` (or deleted/ignored — your call).
4. **Group** — staging is split into album units; bare `CD1`/`Disc 2`/`12 Vinyl 01`
   subfolders merge into their parent album.
5. **Import** — beets + MusicBrainz (tags + acoustic fingerprints) auto-tags and
   files confident matches. Albums with unresolved file problems are held back.
6. **Resolve** — whatever didn't match gets an interactive menu per album:
   full beets TUI, manual placement (gamerips, bootlegs), skip, or delete.
7. **Normalize albumartists** — collab tags like `Bones & cat soup` fragment the
   artist list on Rockbox/Neutron. You pick the canonical primary artist once;
   the folder + albumartist follow it, per-track artist keeps the full credit.
8. **Post-check & sync** — imported files re-verified; optional MP3 mirror sync.

### Pushing the mirror to devices

The MP3 mirror is converted once, locally; devices get a plain rsync. Both steps
are incremental, so adding two albums means seconds of copying, not a full rewrite:

```bash
# iPod (Rockbox), BlackBerry SD card, etc. — always target the device's Music
# folder, NEVER its root (.rockbox lives next to Music/ and --delete would eat it)
rsync -rt --modify-window=2 --delete ~/Music/MP3/ "/run/media/$USER/CENGIZ IPOD/Music/"
```

`-rt` because FAT has no permissions to preserve; `--modify-window=2` because FAT
rounds timestamps to 2 s (without it rsync re-copies everything every run);
`--delete` so library deletions and moves disappear from the device too.

### Decisions are remembered

Every answer is stored in `<library>/.music-tools/decisions.json`, keyed by
**audio content** (FLAC STREAMINFO MD5 / tag-stripped MP3 hash), not by path or
tags. Re-stage the same music years later — after a full restructure, retagging,
or renaming — and it re-files itself with zero questions. The file lives inside
the library so backups carry it. Album lookups also match fuzzily (≥80% of the
same tracks), so an album that gained a bonus track suggests its old decision
as the default.

## Library layout

`Artist/[Year] Album/NN - Title.ext` for everything — release type lives in
tags only (compatible with Plex, Navidrome, Rockbox, Neutron).

```
Artist/[Year] Album/01 - Track Title.flac
Artist/[Year] Album [EP]/01 - ....flac          ← [EP] suffix cosmetic only
Various Artists/[Year] Compilation/01 - ....flac
Artist/[Year] Multi-Disc Album/101 - ....flac   ← disc 1, track 1
                               201 - ....flac   ← disc 2, track 1
```

Multi-disc handling is implemented in [beets/config.yaml](beets/config.yaml)
(`per_disc_numbering` + a `disc_prefix` inline field) and in the organizer.

## Standalone tools

The intake pipeline drives these, but each still works on its own:

| Tool | Purpose |
|---|---|
| `flac_validator/validate.py LIB` | flag non-FLAC audio (extension + magic bytes); `.flac_exempt` lists exempt dirs |
| `corruption_checker/check.py LIB [--jobs N]` | `flac --test` / ffmpeg decode of every file |
| `flac_mp3_sync/sync.py LIB MP3DIR [--dry-run]` | one-way FLAC→MP3 mirror: convert new/changed, delete orphans |
| `strip_non_audio/strip.py DIR [--apply]` | delete junk files (keeps all audio, `.lrc`, images, `.pdf`/`.cue`) |
| `library_organizer/organize.py LIB [--apply]` | tag-based restructure of an existing library (multi-disc flattening, Various Artists, cover art) |
| `beets/config.yaml` | beets config: MusicBrainz autotagger, naming scheme, art |

```bash
# beets, standalone:
venv/bin/beet -c beets/config.yaml import ~/Music/Library/_Staging   # interactive
venv/bin/beet -c beets/config.yaml mbsync                            # re-sync tags by MBID
```

## Philosophy

- Fix the original FLAC library first — everything else derives from it
- Dry-run before anything destructive; quarantine over delete
- The MP3 directory is never a source of truth
- A question answered once is never asked again
