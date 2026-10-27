#!/bin/bash
# Run one bug-sweeper pass headless with Claude Code.
#
#   bin/run.sh [spec]          default spec: spec/bug-sweeper.md
#
# A deliberately small runner: it prepends the loop contract to the spec body, gives the
# agent a receipt path, enforces the spec's max_minutes as a wall-clock timeout, and stops
# early if a STOP file exists. Schedule it with cron or launchd at the hours in the spec.
#
# Environment:
#   BUG_SWEEPER_STATE_DIR   ledger + snapshots (default: ./state)
#   BUG_SWEEPER_RECEIPTS    receipt folder     (default: ./receipts)
#   CLAUDE_BIN              claude executable  (default: claude)

set -uo pipefail
ENGINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEC="${1:-$ENGINE/spec/bug-sweeper.md}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
[ -f "$ENGINE/STOP" ] && { echo "STOP file present; not running."; exit 0; }
[ -f "$SPEC" ] || { echo "no such spec: $SPEC" >&2; exit 2; }

field() { awk -v k="$1" 'NR==1&&/^---/{f=1;next} f&&/^---/{exit} f&&$0 ~ "^"k":"{sub("^"k":[ ]*","");print;exit}' "$SPEC"; }
MODEL="$(field model)"; MAXMIN="$(field max_minutes)"; PERM="$(field permission_mode)"
SLUG="$(field slug)"; SLUG="${SLUG:-bug-sweeper}"

export BUG_SWEEPER_STATE_DIR="${BUG_SWEEPER_STATE_DIR:-$ENGINE/state}"
RECEIPTS="${BUG_SWEEPER_RECEIPTS:-$ENGINE/receipts}"
mkdir -p "$RECEIPTS/$SLUG" "$BUG_SWEEPER_STATE_DIR"
export RECEIPT="$RECEIPTS/$SLUG/$(date '+%Y%m%d-%H%M%S').md"

BODY="$(awk 'NR==1&&/^---/{f=1;next} f&&/^---/{f=0;next} !f' "$SPEC")"
PROMPT="$(cat "$ENGINE/spec/CONTRACT.md")

ENGINE: $ENGINE  (bin/ paths in the spec are relative to it)
RECEIPT: $RECEIPT
MAX_PASSES: $(field max_passes)
OUTBOUND: $(field outbound)

$BODY"

cd "$ENGINE"
echo "# $SLUG — $(date '+%b %-d, %-I:%M %p')" > "$RECEIPT"
timeout_cmd=""; command -v gtimeout >/dev/null && timeout_cmd="gtimeout ${MAXMIN:-40}m"
command -v timeout >/dev/null && timeout_cmd="timeout ${MAXMIN:-40}m"
$timeout_cmd "$CLAUDE_BIN" -p "$PROMPT" ${MODEL:+--model "$MODEL"} \
  ${PERM:+--permission-mode "$PERM"} --output-format text >> "$RECEIPT" 2>&1
code=$?
echo "<!-- status: exit=$code -->" >> "$RECEIPT"
echo "receipt: $RECEIPT"
exit $code
