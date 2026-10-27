#!/bin/bash
# Undo for projects that are not git repos.
#
#   snapshot.sh save <file> [file...]   copy files aside, print the snapshot dir
#   snapshot.sh restore <dir>           put every file in that snapshot back
#   snapshot.sh list                    show snapshots kept
#
# The bug-sweeper must be able to revert a fix that fails verification. In a git repo
# that's `git checkout -- <file>`; many working projects are not
# repos at all, so "revert" would otherwise mean "hope." Snapshot before editing.
#
# Files are stored under their absolute path inside the snapshot dir, so restore needs
# no bookkeeping and cannot put a file back in the wrong place.

set -uo pipefail
ENGINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${BUG_SWEEPER_STATE_DIR:-$ENGINE/state}/bug-sweeper.snapshots"

CMD="${1:-}"

case "$CMD" in
  save)
    shift
    [ $# -eq 0 ] && { echo "usage: snapshot.sh save <file> [file...]" >&2; exit 2; }
    DIR="$ROOT/$(date '+%Y%m%d-%H%M%S')-$$"
    for f in "$@"; do
      [ -f "$f" ] || { echo "no such file: $f" >&2; exit 3; }
      ABS="$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"
      mkdir -p "$DIR$(dirname "$ABS")"
      cp -p "$ABS" "$DIR$ABS" || exit 3
    done
    echo "$DIR"
    ;;

  restore)
    DIR="${2:-}"
    [ -d "$DIR" ] || { echo "no such snapshot: $DIR" >&2; exit 2; }
    n=0
    while IFS= read -r saved; do
      target="${saved#$DIR}"
      cp -p "$saved" "$target" && n=$((n + 1)) && echo "restored $target"
    done < <(find "$DIR" -type f)
    echo "$n file(s) restored"
    ;;

  list)
    [ -d "$ROOT" ] || { echo "(none)"; exit 0; }
    for d in "$ROOT"/*/; do
      [ -d "$d" ] || continue
      echo "$d  ($(find "$d" -type f | wc -l | tr -d ' ') files)"
    done
    ;;

  *)
    sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
