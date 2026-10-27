#!/usr/bin/env python3
"""
Read every Claude Code transcript (and Codex history) and surface the bugs the user reported in passing.

    scan-chats.py [--days N] [--all] [--json]

The user says "the screen is black" or "it wont let me press enter" mid-conversation, the
session moves on, and the complaint is never picked back up. This walks
~/.claude/projects/*/*.jsonl, finds those moments, and prints them as structured
findings so the bug-sweeper loop can go fix them.

What counts as a chat message:
  - type "user" with promptSource typed/queued/suggestion_accepted — words the user actually
    typed. Everything else in a transcript is tool results, system notices, or
    injected context, and none of that is him reporting anything.
  - entrypoint "sdk-cli" is excluded: those are headless `claude -p` runs, i.e. the
    loops themselves. Without this the sweeper reads its own transcripts and reports
    its own output back to itself as new bugs.

Scoring, not keyword matching. "break this down into a file" and "break out into the
millions" both contain "break"; neither is a bug. Signals are weighted and context
patterns subtract, so a finding has to clear a threshold to be emitted at all.

SECURITY: transcript text is data, never instructions. It is quoted into the loop
prompt and a transcript can contain arbitrary text — including text addressed to an
AI. The consumer must treat every quote as a report to investigate, never as a command.
"""

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sys

HOME = os.path.expanduser("~")
# Every location is overridable so the scanner can run against fixtures or another machine's layout.
PROJECTS = os.environ.get("CLAUDE_PROJECTS_DIR") or os.path.join(HOME, ".claude", "projects")
# Codex CLI's history.jsonl holds exactly the prompts the user typed (no injected context),
# so weeks spent in Codex are not invisible here.
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
CODEX_HISTORY = os.path.join(CODEX_HOME, "history.jsonl")
CODEX_SESSIONS = os.path.join(CODEX_HOME, "sessions")
ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.environ.get("BUG_SWEEPER_STATE_DIR") or os.path.join(ENGINE, "state")
LEDGER = os.environ.get("LEDGER_FILE") or os.path.join(STATE, "bug-sweeper.ledger.json")

HUMAN_SOURCES = {"typed", "queued", "suggestion_accepted"}

# Unambiguous "something is wrong right now" language. Weight 2 — one is enough.
STRONG = [
    r"do(?:es)?n'?t work", r"not work(?:ing)?", r"isn'?t work(?:ing)?", r"stopped working",
    r"won'?t (?:let|load|open|run|start|build|work|save|submit)", r"not letting me",
    r"\bstill (?:not|doesn'?t|isn'?t|broken|failing|wrong)\b",
    r"\bbroken\b", r"\bcrash(?:es|ed|ing)?\b", r"\bbug\b",
    r"\berror\b", r"\bexception\b", r"\btraceback\b",
    r"\bfail(?:s|ed|ing)?\b", r"\bhangs?\b", r"\bfrozen\b", r"\bfreezes?\b",
    r"screen is (?:black|blank|white|empty)", r"\b(?:page|screen) (?:is )?blank\b",
    r"nothing happens", r"no longer works", r"stuck (?:on|at|in)",
    r"please fix", r"fix (?:this|that|it|the)\b", r"needs? (?:to be )?fixed",
]

# Suggestive but ambiguous on its own. Weight 1 — needs a second signal.
WEAK = [
    r"\bissue\b", r"\bproblem\b", r"\bwrong\b", r"\bglitch\b", r"\bweird\b",
    r"\bmissing\b", r"\bterrible\b", r"\bmessed up\b", r"\boff\b",
    r"why is", r"why does", r"why won'?t", r"why isn'?t", r"how come",
    r"\bshould(?:n'?t)? (?:be|have|show)\b", r"\bsupposed to\b",
]

# Idioms that merely contain a signal word. Each subtracts 2 — enough to cancel one.
NEGATIVE = [
    # "break this all down", "break it down" — up to 3 words may sit in the middle.
    r"break\s+(?:\w+\s+){0,3}down", r"\bbreakdown\b", r"break out",
    r"break(?:ing)? into", r"\bbreakthrough\b", r"take a break",
    r"error handling", r"error states?", r"empty,? loading", r"loading,? (?:success|error)",
    r"no problem", r"if (?:there'?s|any) (?:an? )?(?:issue|problem|error)",
    r"fix(?:ing)? (?:up )?the (?:copy|wording|design|spacing|colors?)",
]

# The user confirming the thing got sorted, later in the same session.
RESOLVED = [
    r"\b(?:works?|working) now\b", r"\bthat(?:'?s| is) it\b", r"\bperfect\b",
    r"\bfixed\b", r"\bgot it\b", r"\bnice\b", r"\bbeautiful\b", r"\blooks good\b",
    r"\bthank(?:s| you)\b", r"\bawesome\b", r"\bthat worked\b", r"\byes+\b",
]

STRONG_RE = [re.compile(p, re.I) for p in STRONG]
WEAK_RE = [re.compile(p, re.I) for p in WEAK]
NEGATIVE_RE = [re.compile(p, re.I) for p in NEGATIVE]
RESOLVED_RE = [re.compile(p, re.I) for p in RESOLVED]

THRESHOLD = 2

# Score only the opening of a message. The user leads with the report and pastes the
# evidence after it ("here's what xcode says: <500 lines>"). Scoring the whole body
# meant a pasted error log, or a pasted draft *about* bugs, scored higher than an
# actual complaint — the worst false positive in the first calibration run was a
# pasted App Store write-up that mentioned "fails", "broken" and "crash".
LEAD_CHARS = 400


def score(text):
    """Weighted signal score plus the names of what matched, for explainability."""
    text = text[:LEAD_CHARS]
    hits, total = [], 0
    for rx in STRONG_RE:
        if rx.search(text):
            total += 2
            hits.append(rx.pattern)
    for rx in WEAK_RE:
        if rx.search(text):
            total += 1
            hits.append(rx.pattern)
    for rx in NEGATIVE_RE:
        if rx.search(text):
            total -= 2
            hits.append("-" + rx.pattern)
    return total, hits


def human_time(iso):
    """ISO timestamp -> "Jul 24, 7:51 PM" in local time, 12-hour clock."""
    if not iso:
        return "?"
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except Exception:
        return iso[:16]
    return f"{t.strftime('%b')} {t.day}, {t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def load_ledger():
    try:
        with open(LEDGER) as fh:
            return json.load(fh)
    except Exception:
        return {}


def human_messages(path):
    """Yield (index, record) for messages the user actually typed, in file order."""
    try:
        fh = open(path, errors="replace")
    except OSError:
        return
    with fh:
        for i, line in enumerate(fh):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "user" or d.get("entrypoint") == "sdk-cli":
                continue
            if d.get("promptSource") not in HUMAN_SOURCES:
                continue
            content = d.get("message", {}).get("content")
            if isinstance(content, str) and content.strip():
                yield i, d, content


def codex_conversations():
    """Yield (path, msgs) per Codex session from history.jsonl, shaped like human_messages."""
    if not os.path.exists(CODEX_HISTORY):
        return
    sessions = {}
    try:
        fh = open(CODEX_HISTORY, errors="replace")
    except OSError:
        return
    with fh:
        for i, line in enumerate(fh):
            try:
                d = json.loads(line)
            except Exception:
                continue
            text, sid, ts = d.get("text"), d.get("session_id"), d.get("ts")
            if not isinstance(text, str) or not text.strip() or not sid or not ts:
                continue
            iso = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).isoformat().replace("+00:00", "Z")
            sessions.setdefault(sid, []).append((i, {"timestamp": iso, "sessionId": sid}, text))
    # Session files carry the cwd; index them by the session id embedded in the filename.
    files = {}
    for f in glob.glob(os.path.join(CODEX_SESSIONS, "**", "rollout-*.jsonl"), recursive=True):
        files[os.path.basename(f)[-42:-6]] = f
    for sid, msgs in sessions.items():
        path = files.get(sid, CODEX_HISTORY)
        cwd = ""
        if path != CODEX_HISTORY:
            try:
                with open(path, errors="replace") as fh:
                    cwd = (json.loads(fh.readline()).get("payload") or {}).get("cwd") or ""
            except Exception:
                pass
        for _, rec, _ in msgs:
            rec["cwd"] = cwd
        yield path, msgs


def conversations():
    for path in sorted(glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))):
        yield path, list(human_messages(path))
    yield from codex_conversations()


def scan(days, scan_all):
    ledger = load_ledger()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    findings = []

    for path, msgs in conversations():
        if not msgs:
            continue
        texts = [m[2] for m in msgs]

        for pos, (_, rec, text) in enumerate(msgs):
            ts_raw = rec.get("timestamp") or ""
            try:
                ts = dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except Exception:
                continue
            if not scan_all and ts < cutoff:
                continue

            total, hits = score(text)
            if total < THRESHOLD:
                continue

            # Everything the user typed after the complaint, in this same session.
            after = " ".join(texts[pos + 1:])
            resolved = any(rx.search(after) for rx in RESOLVED_RE)
            # A complaint he repeated later is the opposite of resolved.
            repeated = any(score(t)[0] >= THRESHOLD for t in texts[pos + 1:])

            fid = hashlib.sha1(
                f"{rec.get('sessionId')}|{ts_raw}|{text[:200]}".encode()
            ).hexdigest()[:12]
            if fid in ledger:
                continue

            findings.append({
                "id": fid,
                "reportedAt": ts_raw,
                "reportedAtHuman": human_time(ts_raw),
                "project": rec.get("cwd") or "",
                "gitBranch": rec.get("gitBranch") or "",
                "sessionId": rec.get("sessionId") or "",
                "transcript": path,
                "confidence": "high" if total >= 4 else "medium",
                "score": total,
                "signals": hits[:6],
                # Untrusted. A report to investigate, never an instruction.
                "quote": text.strip()[:1200],
                "likelyResolvedInSession": bool(resolved and not repeated),
                "repeatedLater": repeated,
            })

    findings.sort(key=lambda f: f["reportedAt"], reverse=True)
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="lookback window (default 14)")
    ap.add_argument("--all", action="store_true", help="ignore the lookback window")
    ap.add_argument("--json", action="store_true", help="raw JSON instead of a report")
    args = ap.parse_args()

    findings = scan(args.days, args.all)

    if args.json:
        json.dump(findings, sys.stdout, indent=2)
        print()
        return

    open_f = [f for f in findings if not f["likelyResolvedInSession"]]
    print(f"{len(findings)} finding(s) · {len(open_f)} still look open · "
          f"lookback={'all' if args.all else str(args.days) + 'd'}")
    print(f"ledger: {len(load_ledger())} already handled\n")
    for f in findings:
        flag = "RESOLVED?" if f["likelyResolvedInSession"] else "OPEN     "
        rep = " (repeated later)" if f["repeatedLater"] else ""
        print(f"[{flag}] {f['id']}  {human_time(f['reportedAt']):<18}  {f['confidence']}{rep}")
        print(f"           project: {f['project'] or '(unknown)'}")
        print(f"           \"{f['quote'][:150].replace(chr(10), ' ')}\"")
        print()


if __name__ == "__main__":
    main()
