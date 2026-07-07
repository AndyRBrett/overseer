"""Shared fixtures for the fixer tests.

`fake_remote` stands in for GitHub: a local bare git repo seeded with a tiny
project whose test fails (the shape of a real fixable bug — add() subtracts).
`tools._clone_url` is monkeypatched to point at it, so the workspace tools run
their real git plumbing (clone, branch, commit, push) with zero network.
"""

import subprocess

import pytest

import tools as o

# What the seeded "project" looks like: a one-function module with an obvious
# bug and a test that catches it. `python test_calc.py` exits non-zero.
BUGGY_CALC = "def add(a, b):\n    return a - b\n"
FIXED_CALC = "def add(a, b):\n    return a + b\n"
CALC_TEST = (
    "from calc import add\n\n\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5, f'add(2,3) returned {add(2, 3)}'\n\n\n"
    'if __name__ == "__main__":\n'
    "    test_add()\n"
    '    print("ok")\n'
)


def _run(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def fake_remote(tmp_path, monkeypatch):
    """(repo_slug, bare_repo_path) — a configured project repo whose 'GitHub'
    is a local bare repo, seeded with the buggy calc project on `main`."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _run(seed, "git", "init", "-b", "main")
    _run(seed, "git", "config", "user.email", "seed@test")
    _run(seed, "git", "config", "user.name", "seed")
    (seed / "calc.py").write_text(BUGGY_CALC)
    (seed / "test_calc.py").write_text(CALC_TEST)
    _run(seed, "git", "add", "-A")
    _run(seed, "git", "commit", "-m", "seed project with add() bug")
    _run(seed, "git", "remote", "add", "origin", str(origin))
    _run(seed, "git", "push", "-u", "origin", "main")

    repo_slug = "AndyRBrett/ufc-dashboard"
    monkeypatch.setitem(o.PROJECTS["ufc"], "repo", repo_slug)
    monkeypatch.setattr(o, "_clone_url", lambda slug: str(origin))
    o.set_dry_run(False)
    yield repo_slug, origin
    o.cleanup_workspaces()


def remote_branches(origin):
    out = subprocess.run(["git", "--git-dir", str(origin), "branch",
                          "--format=%(refname:short)"],
                         check=True, capture_output=True, text=True).stdout
    return out.split()


def remote_file(origin, branch, path):
    return subprocess.run(["git", "--git-dir", str(origin), "show",
                           f"{branch}:{path}"],
                          check=True, capture_output=True, text=True).stdout
