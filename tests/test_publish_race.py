"""Tests for how ledger-refresh publishes against a moving main.

2026-09-14, run #448: the :20 cron overlapped the weekly review's own publish by
two minutes. `git pull --rebase` conflicted in all three published files — they
are generated end to end, so there is no line either side could win — and the
two remaining retries then died on "Pulling is not possible because you have
unmerged files" without attempting a single push, because nothing had cleaned
the conflicted tree up. Three retries, zero pushes, one red run over files that
were republished an hour later anyway.

A derived file is not merged, it is recomputed: take whatever landed first and
rebuild on top of it. What must NOT happen is the obvious shortcut of forcing
our copy over the remote — ours holds a digest rebuilt from the PREVIOUS one,
and pushing it would roll `generated` back to last week's review (invariant 13),
telling the dead-man's switch a review had run when none had.

The loop is shell inside the workflow, so these tests extract it and run it for
real against two throwaway git repos.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/ledger-refresh.yml"
FILES = ["docs/shipped.json", "docs/ask-context.json", "docs/digest.json"]


def _publish_script():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["refresh"]["steps"]
    return next(s["run"] for s in steps if s.get("name") == "Publish if anything moved")


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check)


def _seed(repo, marker):
    """Write the three published files, as a rebuild would."""
    (repo / "docs").mkdir(exist_ok=True)
    for name in FILES:
        (repo / name).write_text('{"generated": "%s", "from": "%s"}\n' % (marker, name))


@pytest.fixture
def world(tmp_path):
    """A bare origin, our checkout, and a second writer's checkout."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    _git(seed, "config", "user.name", "seed")
    _git(seed, "config", "user.email", "seed@example.com")
    _seed(seed, "monday-0600")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")
    _git(seed, "push", "-q", "origin", "main")

    ours = tmp_path / "ours"
    theirs = tmp_path / "theirs"
    subprocess.run(["git", "clone", "-q", str(origin), str(ours)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(theirs)], check=True)

    # The rebuild the publish step re-runs after losing a race. Stubbed: the
    # real one reads GitHub, but what is being tested is the race handling.
    (ours / "scripts").mkdir()
    (ours / "scripts/rebuild_docs.sh").write_text(
        'set -e\ncd "$(dirname "$0")/.."\n'
        'for f in %s; do printf \'{"generated": "%%s", "from": "%%s"}\\n\' '
        '"refresh-1420" "$f" > "$f"; done\n' % " ".join(FILES)
    )
    return origin, ours, theirs


def _publish(repo):
    return subprocess.run(["bash", "-c", _publish_script()], cwd=repo,
                          capture_output=True, text=True)


def _remote_head_content(origin, tmp_path, name):
    out = subprocess.run(["git", "-C", str(origin), "show", f"main:{name}"],
                         capture_output=True, text=True, check=True)
    return out.stdout


def test_a_clean_publish_lands(world, tmp_path):
    origin, ours, _ = world
    subprocess.run(["bash", "scripts/rebuild_docs.sh"], cwd=ours, check=True)

    result = _publish(ours)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "refresh-1420" in _remote_head_content(origin, tmp_path, "docs/digest.json")


def test_nothing_moved_is_not_a_commit(world, tmp_path):
    origin, ours, _ = world
    before = _git(origin, "rev-parse", "main").stdout

    result = _publish(ours)

    assert result.returncode == 0
    assert "Nothing moved" in result.stdout
    assert _git(origin, "rev-parse", "main").stdout == before


def test_losing_the_race_rebuilds_on_top_instead_of_going_red(world, tmp_path):
    """The 2026-09-14 failure, reproduced: main moves under us mid-run."""
    origin, ours, theirs = world
    subprocess.run(["bash", "scripts/rebuild_docs.sh"], cwd=ours, check=True)

    # The weekly review publishes its digest while we were building ours.
    _git(theirs, "config", "user.name", "review")
    _git(theirs, "config", "user.email", "review@example.com")
    _seed(theirs, "monday-1411-weekly-review")
    _git(theirs, "add", "-A")
    _git(theirs, "commit", "-qm", "Weekly digest")
    _git(theirs, "push", "-q", "origin", "main")
    landed_first = _git(theirs, "rev-parse", "HEAD").stdout.strip()

    result = _publish(ours)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "rebuilding onto the new main" in result.stdout
    # The review's commit is still in history — not merged over, not forced past.
    assert _git(origin, "merge-base", "--is-ancestor", landed_first, "main",
                check=False).returncode == 0
    # And the published file is the recomputed one, not a conflicted merge.
    published = _remote_head_content(origin, tmp_path, "docs/digest.json")
    assert "refresh-1420" in published
    assert "<<<<<<<" not in published


def test_a_race_we_have_nothing_to_add_to_ends_quietly(world, tmp_path):
    """If the rebuild agrees with what landed first, there is nothing to push."""
    origin, ours, theirs = world
    subprocess.run(["bash", "scripts/rebuild_docs.sh"], cwd=ours, check=True)

    # Someone else published exactly what our rebuild produces.
    _git(theirs, "config", "user.name", "review")
    _git(theirs, "config", "user.email", "review@example.com")
    _seed(theirs, "refresh-1420")
    for name in FILES:
        (theirs / name).write_text('{"generated": "refresh-1420", "from": "%s"}\n' % name)
    _git(theirs, "add", "-A")
    _git(theirs, "commit", "-qm", "Refresh delivery ledger and project health")
    _git(theirs, "push", "-q", "origin", "main")

    result = _publish(ours)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "already current" in result.stdout
