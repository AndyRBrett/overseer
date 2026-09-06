"""Assert the Worker Cloudflare actually runs matches the one in wrangler.toml.

THE INCIDENT THIS EXISTS FOR (2026-09-06). The review's off-GitHub trigger fired
a day early because wrangler.toml carried GitHub's day-of-week convention rather
than Cloudflare's. The fix was a two-character edit, merged the same afternoon —
and then took two deploys to go live. The first was never run at all; the second
ran against a checkout four days stale and re-installed the very schedule it was
meant to replace, reporting "Deployed overseer-ask triggers" as it did so.

Both times the only evidence was `wrangler deploy` happening to echo its trigger
lines, read by a human who thought to look. That is not a check. Nothing in this
repo could answer "does Cloudflare agree with main", which is the same class of
blind spot as the dropped cron of 08-31: no failure, no red run, just two
systems quietly disagreeing.

So the deploy asserts it. wrangler prints the schedules it installed; this reads
them back and compares them to the file that was supposed to produce them. A
deploy that installs the wrong schedule is now a red workflow run rather than a
line of output nobody reads.

Deliberately strict about finding NOTHING to compare. An empty parse means the
deploy installed no triggers, or wrangler changed its output format — and a
verifier that passes when it cannot see anything is worse than no verifier, in
exactly the way a dead-man's switch that shares a failure mode with what it
watches is worse than no alarm.

Usage:  python scripts/verify_worker_triggers.py <deploy-log> [wrangler.toml]
"""

import re
import sys

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib

# wrangler prints one "schedule: <cron>" line per installed trigger. Anchored
# loosely because the line arrives indented and possibly colour-coded.
SCHEDULE_LINE = re.compile(r"schedule:\s*(.+?)\s*$")

# Strips the ANSI colour wrangler emits when it thinks it has a terminal.
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def declared_crons(wrangler_path):
    """The crons wrangler.toml asks for."""
    with open(wrangler_path, "rb") as f:
        config = tomllib.load(f)
    return [str(c).strip() for c in (config.get("triggers") or {}).get("crons") or []]


def installed_crons(log_text):
    """The crons wrangler says it installed, read back off its own output."""
    found = []
    for line in ANSI.sub("", log_text).splitlines():
        match = SCHEDULE_LINE.search(line)
        if match:
            found.append(match.group(1).strip())
    return found


def compare(declared, installed):
    """(ok, message). Order is not meaningful; presence is."""
    if not declared:
        return False, "wrangler.toml declares no crons — nothing to verify against."
    if not installed:
        return False, (
            "the deploy output contained no 'schedule:' lines. Either no triggers "
            "were installed or wrangler's output format changed; refusing to pass "
            "a check that cannot see what it is checking."
        )
    if sorted(declared) == sorted(installed):
        return True, "Cloudflare installed exactly the schedule wrangler.toml declares."

    missing = sorted(set(declared) - set(installed))
    extra = sorted(set(installed) - set(declared))
    lines = ["the deployed schedule does not match wrangler.toml."]
    lines.append(f"  declared:  {sorted(declared)}")
    lines.append(f"  installed: {sorted(installed)}")
    if missing:
        lines.append(f"  declared but NOT installed: {missing}")
    if extra:
        lines.append(f"  installed but NOT declared: {extra}")
    lines.append(
        "  A stale checkout deploys the previous schedule and still reports "
        "success — that is how 2026-09-06 stayed broken through a deploy."
    )
    return False, "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: verify_worker_triggers.py <deploy-log> [wrangler.toml]",
              file=sys.stderr)
        return 2
    log_path = argv[0]
    wrangler_path = argv[1] if len(argv) > 1 else "worker/wrangler.toml"

    with open(log_path, encoding="utf-8", errors="replace") as f:
        log_text = f.read()

    ok, message = compare(declared_crons(wrangler_path), installed_crons(log_text))
    print(("OK: " if ok else "MISMATCH: ") + message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
