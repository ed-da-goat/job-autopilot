#!/bin/zsh
# Render launchd/*.plist.template into ~/Library/LaunchAgents with __HOME__ and
# __REPO__ filled in. Does NOT load anything: it prints the launchctl commands.
#
# Usage: scripts/install-launchd.sh [daily|watch|autoapply ...]   (default: daily watch)
# autoapply SUBMITS applications unattended, so it is never in the default set;
# name it explicitly once you have reviewed the dry runs and armed it.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Library/LaunchAgents"
jobs=("$@")
[ ${#jobs[@]} -eq 0 ] && jobs=(daily watch)

mkdir -p "$DEST"
for job in "${jobs[@]}"; do
  src="$REPO/launchd/com.jobhunt.$job.plist.template"
  out="$DEST/com.jobhunt.$job.plist"
  [ -f "$src" ] || { echo "no template for '$job': $src" >&2; exit 1; }
  content="$(<"$src")"
  content="${content//__REPO__/$REPO}"
  content="${content//__HOME__/$HOME}"
  print -r -- "$content" > "$out"
  echo "wrote $out"
done

echo
echo "Nothing is loaded yet. To activate, run:"
for job in "${jobs[@]}"; do
  echo "  launchctl unload $DEST/com.jobhunt.$job.plist 2>/dev/null; launchctl load $DEST/com.jobhunt.$job.plist"
done
