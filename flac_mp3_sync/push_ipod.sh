#!/bin/bash
# Push the MP3 mirror to a mounted Rockbox iPod, gently.
#
#   push_ipod.sh <ipod mountpoint> [mirror]      mirror default: ev:/media/devmon/Expansion/Music/Official_mp3
#
#   - music goes to <ipod>/Music/, the playlists (playlists.py's Rockbox copy,
#     paths /Music/...) to <ipod>/Playlists/ where Rockbox's Playlists menu
#     looks; the relative .m3u8 copies at the mirror root are for other
#     devices and are kept off the iPod
#   - one attempt, no retries, capped at 1.5 MB/s: a dropped USB link in the
#     middle of a write damages the FAT, and retrying on top of that only
#     makes it worse (run fsck.vfat, then push again)
#   - playlists on the iPod that this tool did not write are left alone
#
# Never target the iPod root: .rockbox lives there and --delete would eat it.
# Charge the iPod on a wall charger first and use Disk Mode.
set -u
IPOD=${1:?usage: push_ipod.sh <ipod mountpoint> [mirror]}
MIRROR=${2:-ev:/media/devmon/Expansion/Music/Official_mp3}
[ -d "$IPOD/.rockbox" ] || { echo "no .rockbox in $IPOD, not an iPod mount?"; exit 1; }
SSH="ssh -o ClearAllForwardings=yes -o ForwardX11=no -o ConnectTimeout=30"
MARKER="exported from Plex by flac_mp3_sync/playlists.py"
RS=(rsync -rt --modify-window=2 -e "$SSH" --rsync-path="ionice -c3 nice -n19 rsync")

"${RS[@]}" --delete-during --partial --bwlimit=1500 --stats \
    --exclude='/*.m3u8' --exclude='/Playlists/' "$MIRROR/" "$IPOD/Music/" || exit $?

mkdir -p "$IPOD/Playlists"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
"${RS[@]}" --include='*.m3u8' --exclude='*' "$MIRROR/Playlists/" "$TMP/" || exit $?
for f in "$IPOD/Playlists"/*.m3u8; do
    [ -e "$f" ] || continue
    [ -e "$TMP/$(basename "$f")" ] && continue
    head -c 500 "$f" | grep -q "$MARKER" && rm -f "$f" && echo "removed stale playlist $(basename "$f")"
done
cp "$TMP"/*.m3u8 "$IPOD/Playlists/" 2>/dev/null
echo "playlists: $(ls "$TMP"/*.m3u8 2>/dev/null | wc -l) in $IPOD/Playlists"
sync
