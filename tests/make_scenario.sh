#!/usr/bin/env bash
# Build an isolated scenario workspace for intake testing.
#
#   tests/make_scenario.sh NAME "CLONE_REL_PATH" ["CLONE_REL_PATH"...]
#
# Creates sample_library/scenarios/NAME/{library/_Staging,mp3} + intake.toml,
# copying each given path from the pristine clone into _Staging.
# Prints the workspace path. NEVER writes to the clone.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
NAME="$1"; shift
WS="$REPO/sample_library/scenarios/$NAME"
CLONE="$REPO/sample_library/clone"
rm -rf "$WS"
mkdir -p "$WS/library/_Staging" "$WS/mp3"
for rel in "$@"; do
  src="$CLONE/$rel"
  base="$(basename "$rel")"
  if [ -d "$src" ]; then
    rsync -a "$src/" "$WS/library/_Staging/$base/"
  else
    cp "$src" "$WS/library/_Staging/"
  fi
done
cat > "$WS/intake.toml" <<EOF
library = "$WS/library"
mp3_target = "$WS/mp3"
bitrate = "192k"
jobs = 4
EOF
echo "$WS"
