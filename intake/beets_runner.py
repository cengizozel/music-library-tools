#!/usr/bin/env python3
"""
Thin wrapper around the `beet` CLI for the intake pipeline.

All invocations use an *effective config*: the repo's beets/config.yaml merged
with per-run overrides (library directory, db path, optional match-threshold
relaxation for replays), written to a temp file. The repo yaml stays pristine
and standalone-usable.

Field separator for queries is U+001F (unit separator) — album titles can
contain anything printable.
"""

import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

SEP = "\x1f"
ALBUM_FMT = SEP.join(["$added", "$id", "$mb_albumid", "$albumartist", "$album", "$year", "$path"])
ITEM_FMT = SEP.join(["$disc", "$track", "$title", "$path"])


class BeetsRunner:
    def __init__(self, beet_bin: Path, base_config: Path, library_dir: Path,
                 db_path: Path, fpcalc: Path | None = None, log_path: Path | None = None):
        self.beet_bin = str(beet_bin)
        self.base_config = Path(base_config)
        self.library_dir = Path(library_dir)
        self.db_path = Path(db_path)
        self.fpcalc = fpcalc
        self.log_path = log_path
        self._tmpdir = tempfile.TemporaryDirectory(prefix="intake-beets-")
        self._configs: dict[str, str] = {}

    def _env(self) -> dict:
        env = os.environ.copy()
        if self.fpcalc:
            env["FPCALC"] = str(self.fpcalc)
            env["PATH"] = f"{Path(self.fpcalc).parent}:{env.get('PATH', '')}"
        return env

    def _config(self, relax: bool = False) -> str:
        """Effective config file: base yaml + per-run overrides, cached per variant."""
        key = "relax" if relax else "normal"
        if key not in self._configs:
            cfg = yaml.safe_load(self.base_config.read_text())
            if self.log_path:
                cfg.setdefault("import", {})["log"] = str(self.log_path)
            # The pipeline parses beet output; colors only get in the way.
            cfg.setdefault("ui", {})["color"] = False
            if relax:
                # Replays force a stored MusicBrainz ID; trust it even when the
                # rip's tags are bad enough that the normal threshold would skip.
                cfg.setdefault("match", {})["strong_rec_thresh"] = 0.50
            path = Path(self._tmpdir.name) / f"config-{key}.yaml"
            path.write_text(yaml.safe_dump(cfg, allow_unicode=True))
            self._configs[key] = str(path)
        return self._configs[key]

    def _base_args(self, relax: bool = False) -> list[str]:
        return [
            self.beet_bin,
            "-c", self._config(relax),
            "-d", str(self.library_dir),
            "-l", str(self.db_path),
        ]

    def _run(self, args: list[str], *, interactive: bool = False, relax: bool = False,
             timeout: int | None = 900) -> subprocess.CompletedProcess:
        cmd = self._base_args(relax) + args
        if interactive:
            # Full beets TUI: inherit the terminal.
            return subprocess.run(cmd, env=self._env())
        try:
            return subprocess.run(cmd, env=self._env(), capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # degrade like any failed invocation instead of crashing the run
            return subprocess.CompletedProcess(
                cmd, 124, "", f"beets timed out after {timeout}s")

    # --- imports ---

    def quiet_import(self, path: Path, search_id: str | None = None) -> subprocess.CompletedProcess:
        args = ["import", "-q"]
        relax = False
        if search_id:
            args += ["-S", search_id]
            relax = True
        args.append(str(path))
        return self._run(args, relax=relax)

    def interactive_import(self, path: Path) -> int:
        return self._run(["import", str(path)], interactive=True).returncode

    # --- queries ---

    def albums_added_since(self, since: datetime) -> list[dict]:
        result = self._run(["ls", "-a", "-f", ALBUM_FMT])
        albums = []
        for line in (result.stdout or "").splitlines():
            parts = line.split(SEP)
            if len(parts) != 7:
                continue
            added, beets_id, mbid, albumartist, album, year, path = parts
            try:
                added_dt = datetime.strptime(added, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if added_dt >= since:
                albums.append({
                    "added": added_dt, "id": beets_id, "mb_albumid": mbid,
                    "albumartist": albumartist, "album": album, "year": year,
                    "path": path,
                })
        albums.sort(key=lambda a: a["added"])
        return albums

    def all_albums(self) -> list[dict]:
        return self.albums_added_since(datetime.min)

    def album_by_mbid(self, mbid: str) -> dict | None:
        result = self._run(["ls", "-a", "-f", ALBUM_FMT, f"mb_albumid:{mbid}"])
        for line in (result.stdout or "").splitlines():
            parts = line.split(SEP)
            if len(parts) == 7:
                return {"added": parts[0], "id": parts[1], "mb_albumid": parts[2],
                        "albumartist": parts[3], "album": parts[4],
                        "year": parts[5], "path": parts[6]}
        return None

    def items_for_album(self, beets_album_id: str) -> list[dict]:
        result = self._run(["ls", "-f", ITEM_FMT, f"album_id:{beets_album_id}"])
        items = []
        for line in (result.stdout or "").splitlines():
            parts = line.split(SEP)
            if len(parts) != 4:
                continue
            disc, track, title, path = parts
            items.append({"disc": _to_int(disc), "track": _to_int(track),
                          "title": title, "path": path})
        return items

    def album_fields(self, beets_album_id: str) -> dict | None:
        result = self._run(["ls", "-a", "-f", ALBUM_FMT, f"id:{beets_album_id}"])
        line = (result.stdout or "").strip().splitlines()
        if not line:
            return None
        parts = line[0].split(SEP)
        if len(parts) != 7:
            return None
        return {"id": parts[1], "mb_albumid": parts[2], "albumartist": parts[3],
                "album": parts[4], "year": parts[5], "path": parts[6]}

    # --- modifications ---

    def set_albumartist(self, beets_album_id: str, value: str) -> subprocess.CompletedProcess:
        return self._run(["modify", "-y", "-a", f"albumartist={value}",
                          f"id:{beets_album_id}"])


def _to_int(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        return 0
