#!/usr/bin/env python3
"""
The bug-sweeper's memory of which findings it has already dealt with.

    ledger.py mark <id> <status> "note"     record an outcome
    ledger.py list [status]                 show entries
    ledger.py get <id>                      one entry as JSON

Statuses:
    fixed           edited and verified — the change is in the working tree
    unreproducible  couldn't find it in the code; not guessed at
    wontfix         a request or a preference, not a defect
    needs-user      real, but needs a decision, a credential, or a device
    reverted        a fix was attempted, failed verification, and was rolled back

scan-chats.py skips every id in here, so an entry is what stops the loop from
working the same complaint over and over. Marking is not optional.
"""

import json
import os
import sys
import datetime as dt

ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# LEDGER_FILE lets other loops (e.g. promise-sweeper) point this same CLI at their
# own ledger instead of bug-sweeper's. Unset -> the default below.
STATE = os.environ.get("BUG_SWEEPER_STATE_DIR") or os.path.join(ENGINE, "state")
PATH = os.environ.get("LEDGER_FILE") or os.path.join(STATE, "bug-sweeper.ledger.json")

VALID = {"fixed", "unreproducible", "wontfix", "needs-user", "reverted"}


def human_time(iso):
    """Stored ISO -> "Jul 24, 7:51 PM". 12-hour, because that is how the owner reads time."""
    try:
        t = dt.datetime.fromisoformat(iso)
    except Exception:
        return (iso or "")[:16]
    return f"{t.strftime('%b')} {t.day}, {t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def load():
    try:
        with open(PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save(d):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=2, sort_keys=True)
    os.replace(tmp, PATH)  # atomic: a half-written ledger would re-open every bug


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(2)
    cmd = sys.argv[1]
    d = load()

    if cmd == "mark":
        if len(sys.argv) < 4:
            print("usage: ledger.py mark <id> <status> [note]", file=sys.stderr)
            sys.exit(2)
        fid, status = sys.argv[2], sys.argv[3]
        note = sys.argv[4] if len(sys.argv) > 4 else ""
        if status not in VALID:
            print(f"bad status {status!r}; must be one of {sorted(VALID)}", file=sys.stderr)
            sys.exit(2)
        d[fid] = {
            "status": status,
            "note": note,
            "at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        save(d)
        print(f"{fid} -> {status}")

    elif cmd == "list":
        want = sys.argv[2] if len(sys.argv) > 2 else None
        rows = [(k, v) for k, v in sorted(d.items(), key=lambda kv: kv[1].get("at", ""))
                if not want or v.get("status") == want]
        if not rows:
            print("(empty)")
            return
        for k, v in rows:
            print(f"{k}  {v.get('status','?'):<14} {human_time(v.get('at','')):<18}  {v.get('note','')[:70]}")

    elif cmd == "get":
        print(json.dumps(d.get(sys.argv[2], {}), indent=2))

    else:
        print(__doc__.strip())
        sys.exit(2)


if __name__ == "__main__":
    main()
