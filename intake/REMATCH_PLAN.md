# rematch helper — implementation plan

A small tool to correct a wrong album↔MusicBrainz match after import. Not yet
built; this file is the spec so it can be implemented later in one pass.

## Why it's needed

A wrong match (whether beets auto-matched it or an ID was seeded) lives in TWO
places:

1. The library files — wrong tags written, filed under the wrong
   `Artist/[Year] Album/` folder.
2. `<library>/.music-tools/decisions.json` — the wrong `mbid` is remembered,
   keyed by the album's audio content (`album_key`). If only the files are
   fixed, a future re-stage replays the same wrong match.

`intake.py` only ever processes `_Staging/`, so editing decisions.json alone
does nothing to an album already in the library. The correct fix is: fix the
decision + move files back to staging + re-import. This tool automates that.

## CLI

```
venv/bin/python intake/rematch.py "ALBUM QUERY" --mbid <correct-mb-release-id> [--config intake.toml]
venv/bin/python intake/rematch.py "ALBUM QUERY" --interactive    # pick match in beets TUI instead of an mbid
venv/bin/python intake/rematch.py "ALBUM QUERY" --dry-run
```

- `ALBUM QUERY` is a beets query (e.g. `album:Relapse albumartist:Eminem`) that
  must resolve to exactly ONE album in the beets db; abort if 0 or >1 match
  (print the candidates so the user can narrow it).
- `--mbid` forces the corrected release; `--interactive` instead drops into the
  beets TUI so the user picks/searches.

## Steps the tool performs

Reuse existing modules: `BeetsRunner`, `DecisionStore`, `audio_keys.track_key`,
`audio_keys.album_key`, and `intake`'s grouping/placement where helpful.

1. **Locate the album** in beets via the query (`BeetsRunner.album_fields` +
   an id lookup). Capture its current `path`, `id`, and `mb_albumid`.
2. **Compute its content keys** — `track_key()` over the album's audio files
   (use `runner.items_for_album(id)` to get paths). Derive the old `album_key`.
3. **Purge the stale decision** — remove the matching entry from
   `decisions.json` `albums` (match by `album_key`, or fall back to scanning for
   an entry whose `track_keys` overlap ≥80%). Save the store.
   - Keep a backup copy first (`decisions.backup-rematch-<stamp>.json`), mirroring
     the Koma-incident precedent.
4. **Eject from beets** — `beet remove -a -f id:<id>` (removes the db row, KEEPS
   the files; never pass `-d`).
5. **Move files back to staging** — move the album folder into `_Staging/` under
   a temp dir name. Carry companions (.lrc, cover art, .cue/.pdf) along.
6. **Re-import**:
   - `--mbid`: `runner.quiet_import(staging_dir, search_id=mbid)` (relaxed
     threshold path already exists in BeetsRunner).
   - `--interactive`: `runner.interactive_import(staging_dir)`.
7. **Record the corrected decision** — reuse the `_record_import` logic
   (recompute keys from the newly-imported files, store `action=mbid` with the
   correct mbid). This is the canonical path so it stays consistent with intake.
8. **Re-attach companions + device-prep cover** — call `_attach_lrc`,
   `apply_cover_policy` (pass the corrected `mbid`), `normalize_album_covers` on
   the new album dir, same as intake's post phase.
9. **Cleanup** — remove the emptied temp staging dir; print a before/after
   summary (old album/path → new album/path, mbid change).

## Edge cases to handle

- Query matches multiple/zero albums → print candidates, exit non-zero, change
  nothing.
- The wrong and corrected mbid are identical → no-op with a message.
- Album currently has an `asis` (manual-placement) decision rather than `mbid` →
  still works: purge it and re-import with the new id.
- Lock the run with the existing `<library>/.music-tools/intake.lock` mechanism
  so it can't race a concurrent intake.
- `--dry-run` prints all 9 steps' intended actions and touches nothing.

## Tests (tests/test_intake_units.py or a new file)

- decision purge: an entry is removed by album_key; an overlapping-by-≥80% entry
  is also matched.
- backup file is written before mutation.
- dry-run makes no filesystem/db/store changes.
- (integration, skipped if no network) full rematch on a cloned sample album
  from one mbid to another, asserting the new tags/folder and the corrected
  decision entry.

## Notes

- The whole point is that `decisions.json` ends up with the RIGHT mbid for the
  album's content keys, so a future restructure/re-stage replays correctly.
- Keep it consistent with intake's existing helpers rather than re-implementing
  tag-writing/placement — import from `intake.py` where practical.
