"""Every GitHub-published action is pinned by SHA, not by a moving tag.

Dependabot #53 and #54 are why this file exists. Both were correct bumps —
setup-python v5 -> v7, setup-node v4 -> v7 — and both would have re-landed a
FLOATING tag in .github/workflows/implementer.yml, because Dependabot preserves
whatever format it finds and those two lines were the last floating ones left.

That file is the worst place in the repo for a mutable reference: it is the
reusable workflow all four project repos call, and the job it sets up runs a
coding agent with Bash, a `contents: write` token and an Anthropic key. Whoever
can move a tag chooses what runs there.

Pinning is a convention, and a convention that lives only in reviewers' heads is
one a bot will quietly undo. So it is checked.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml")) + \
    sorted((ROOT / "examples").rglob("*.yml"))

# owner/repo@ref, ignoring local (./) and reusable-workflow references.
USES = re.compile(r"uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@(\S+)")
SHA = re.compile(r"^[0-9a-f]{40}$")

# The one deliberate exception, recorded rather than silently skipped.
#
# anthropics/claude-code-action is the coding agent itself, and it is published
# to be consumed as @v1 — the major tag is where fixes to the agent land. Pinning
# it to a SHA would freeze the agent at whatever it was the day someone last
# looked, which for the thing running unattended every Monday is the worse of
# the two risks. Revisit if that ever stops being true.
FLOATING_BY_DESIGN = {"anthropics/claude-code-action"}


def _references():
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            match = USES.search(line)
            if not match:
                continue
            action, ref = match.groups()
            if action.startswith("AndyRBrett/"):
                continue          # our own reusable workflow, pinned to a branch
            yield path.name, action, ref, line


def test_there_are_references_to_check():
    # A regex that silently matches nothing would make every test below pass.
    assert list(_references())


@pytest.mark.parametrize("name,action,ref,line", list(_references()),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_third_party_actions_are_pinned_by_sha(name, action, ref, line):
    if action in FLOATING_BY_DESIGN:
        pytest.skip(f"{action} is floating by design — see FLOATING_BY_DESIGN")
    assert SHA.match(ref), (
        f"{name} uses {action}@{ref} — a moving reference. Pin the 40-character "
        f"commit SHA and put the version in a trailing comment:\n  {line.strip()}"
    )


@pytest.mark.parametrize("name,action,ref,line", list(_references()),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_a_pinned_sha_says_which_version_it_is(name, action, ref, line):
    # A bare 40-character SHA is unreadable and un-reviewable: nobody can tell
    # whether it is a year out of date. The trailing comment is what makes the
    # pin auditable, and it is the only thing Dependabot has to go on.
    if action in FLOATING_BY_DESIGN:
        pytest.skip(f"{action} is floating by design")
    assert re.search(r"#\s*v\d", line), (
        f"{name} pins {action} with no version comment: {line.strip()}")
