"""Tests for the Fixer's workspace tools.

The two hard guarantees are enforced in code, not just in the agent prompt, so
they get direct tests: only configured project repos can be touched, and the
default branch can never be committed to or pushed. Everything else covers the
plumbing the Fixer relies on: clone/branch setup, command runs, file IO with
traversal guards, real pushes to a (local) remote, PR bodies, dry-run
interception, and token scrubbing.
"""

from types import SimpleNamespace

import agent_fixer
import tools as o
from tests.conftest import BUGGY_CALC, FIXED_CALC, remote_branches, remote_file


class FakeGithub:
    """Stands in for the PyGithub client where a test needs the API surface."""

    def __init__(self, issues=(), open_prs=()):
        self.pulls = []
        self.comments = []
        self.issues = list(issues)
        self.open_prs = list(open_prs)

    def get_repo(self, slug):
        return self

    def get_issues(self, state):
        return self.issues

    def get_pulls(self, state):
        return self.open_prs

    def create_pull(self, **kw):
        self.pulls.append(kw)
        return SimpleNamespace(number=99, html_url="https://example.test/pr/99")

    def get_issue(self, number):
        gh = self

        def create_comment(text):
            gh.comments.append((number, text))
            return SimpleNamespace(html_url=f"https://example.test/issue/{number}#c1")

        def edit(**kw):
            gh.edits.append((number, kw))

        self.edits = getattr(self, "edits", [])
        return SimpleNamespace(create_comment=create_comment, edit=edit,
                               html_url=f"https://example.test/issue/{number}")


# ── guards ───────────────────────────────────────────────────────────────


def test_setup_refuses_unconfigured_repo():
    r = o.setup_fix_workspace("evil/other-repo", 1)
    assert r["status"] == "error" and "not a configured project repo" in r["detail"]


def test_comment_refuses_unconfigured_repo():
    r = o.comment_on_issue("evil/other-repo", 1, "hi")
    assert r["status"] == "error"


def test_commit_refused_on_default_branch(fake_remote):
    repo, origin = fake_remote
    o.setup_fix_workspace(repo, 7)
    assert o.run_in_workspace(repo, "git checkout main")["exit_code"] == 0
    o.write_workspace_file(repo, "calc.py", FIXED_CALC)
    r = o.commit_and_push(repo, "sneaky direct-to-main commit")
    assert r["status"] == "refused"
    assert remote_branches(origin) == ["main"]  # nothing reached the remote
    assert remote_file(origin, "main", "calc.py") == BUGGY_CALC


def test_workspace_path_traversal_refused(fake_remote):
    repo, _ = fake_remote
    o.setup_fix_workspace(repo, 7)
    assert o.read_workspace_file(repo, "../../etc/passwd")["status"] == "error"
    assert o.write_workspace_file(repo, "../escape.txt", "x")["status"] == "error"


# ── workspace plumbing ───────────────────────────────────────────────────


def test_setup_clones_and_creates_fix_branch(fake_remote):
    repo, _ = fake_remote
    r = o.setup_fix_workspace(repo, 7)
    assert r["status"] == "ok"
    assert r["branch"].startswith("overseer/fix-7-")
    assert r["default_branch"] == "main"
    assert "calc.py" in r["files"] and "test_calc.py" in r["files"]


def test_run_in_workspace_reproduces_the_bug(fake_remote):
    repo, _ = fake_remote
    o.setup_fix_workspace(repo, 7)
    r = o.run_in_workspace(repo, "python test_calc.py")
    assert r["status"] == "ok"
    assert r["exit_code"] != 0                       # the seeded bug fails the test
    assert "add(2,3) returned -1" in r["output"]     # real evidence, not a guess


def test_run_in_workspace_timeout(fake_remote):
    repo, _ = fake_remote
    o.setup_fix_workspace(repo, 7)
    assert o.run_in_workspace(repo, "sleep 5", timeout=1)["status"] == "timeout"


def test_run_in_workspace_truncates_long_output(fake_remote):
    repo, _ = fake_remote
    o.setup_fix_workspace(repo, 7)
    r = o.run_in_workspace(repo, 'python -c "print(\'x\' * 50000)"')
    assert r["truncated"] is True
    assert len(r["output"]) < 50_000


def test_command_output_never_leaks_the_token(fake_remote, monkeypatch):
    repo, _ = fake_remote
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_sekret12345")
    o.setup_fix_workspace(repo, 7)
    r = o.run_in_workspace(repo, "echo url-with-ghp_sekret12345-inside")
    assert "ghp_sekret12345" not in r["output"]
    assert "***" in r["output"]


def test_read_write_roundtrip(fake_remote):
    repo, _ = fake_remote
    o.setup_fix_workspace(repo, 7)
    assert o.read_workspace_file(repo, "calc.py")["content"] == BUGGY_CALC
    o.write_workspace_file(repo, "calc.py", FIXED_CALC)
    assert o.read_workspace_file(repo, "calc.py")["content"] == FIXED_CALC
    assert o.read_workspace_file(repo, "nope.py")["status"] == "error"


def test_commit_and_push_lands_on_fix_branch_only(fake_remote):
    repo, origin = fake_remote
    ws = o.setup_fix_workspace(repo, 7)
    o.write_workspace_file(repo, "calc.py", FIXED_CALC)
    r = o.commit_and_push(repo, "fix: add() subtracted instead of adding")
    assert r["status"] == "pushed" and r["branch"] == ws["branch"]
    branches = remote_branches(origin)
    assert ws["branch"] in branches
    assert remote_file(origin, ws["branch"], "calc.py") == FIXED_CALC
    assert remote_file(origin, "main", "calc.py") == BUGGY_CALC  # main untouched


def test_open_pull_request_requires_a_pushed_branch(fake_remote, monkeypatch):
    repo, _ = fake_remote
    monkeypatch.setattr(o, "_github", FakeGithub)
    o.setup_fix_workspace(repo, 7)
    r = o.open_pull_request(repo, "Fix add()", "body", 7)
    assert r["status"] == "error" and "not pushed" in r["detail"]


def test_open_pull_request_links_the_issue(fake_remote, monkeypatch):
    repo, _ = fake_remote
    gh = FakeGithub()
    monkeypatch.setattr(o, "_github", lambda: gh)
    ws = o.setup_fix_workspace(repo, 7)
    o.write_workspace_file(repo, "calc.py", FIXED_CALC)
    o.commit_and_push(repo, "fix add()")
    r = o.open_pull_request(repo, "Fix add()", "Root cause: subtraction.", 7)
    assert r["status"] == "opened" and r["url"].endswith("/pr/99")
    (pull,) = gh.pulls
    assert pull["head"] == ws["branch"] and pull["base"] == "main"
    assert "Fixes #7" in pull["body"]  # closes the issue on merge


def test_shell_git_push_cannot_use_credentials(fake_remote, monkeypatch):
    # run_in_workspace lets the agent run arbitrary git, so the clone's remote
    # must hold no credentials — otherwise a shell `git push origin main` would
    # bypass commit_and_push's default-branch guard.
    repo, origin = fake_remote
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_sekret12345")
    o.setup_fix_workspace(repo, 7)
    # The stored URL (git may rewrite the effective one via insteadOf rules):
    url = o.run_in_workspace(repo, "git config remote.origin.url")["output"].strip()
    assert url == f"https://github.com/{repo}.git"       # not the clone source, no token
    o.write_workspace_file(repo, "calc.py", FIXED_CALC)
    o.run_in_workspace(repo, "git add -A && git commit -m sneaky")
    r = o.run_in_workspace(repo, "git push origin HEAD", timeout=30)
    assert r["status"] == "timeout" or r["exit_code"] != 0
    assert remote_branches(origin) == ["main"]           # nothing reached the remote


def test_open_pull_request_enforces_fix_budget(fake_remote, monkeypatch):
    # FIXER_MAX_FIXES is a hard cap in the tool, not advice in the prompt: with
    # a budget of 1, the second PR is refused no matter what the agent decides.
    repo, _ = fake_remote
    gh = FakeGithub()
    monkeypatch.setattr(o, "_github", lambda: gh)
    monkeypatch.setattr(o, "FIXER_MAX_FIXES", 1)
    o.reset_fix_run()
    o.setup_fix_workspace(repo, 7)
    o.write_workspace_file(repo, "calc.py", FIXED_CALC)
    o.commit_and_push(repo, "fix add()")
    assert o.open_pull_request(repo, "Fix add()", "body", 7)["status"] == "opened"
    r = o.open_pull_request(repo, "Another fix", "body", 8)
    assert r["status"] == "refused" and "budget" in r["detail"].lower()
    assert len(gh.pulls) == 1


def test_list_open_issues_surfaces_open_fix_prs(fake_remote, monkeypatch):
    # The prompt tells the fixer to skip issues that already have an open
    # overseer/fix-* PR, so the tool must actually surface those PRs.
    from datetime import datetime, timezone
    repo, _ = fake_remote
    issue = SimpleNamespace(number=7, title="add() wrong", body="details",
                            labels=[], html_url="https://example.test/i/7",
                            created_at=datetime.now(timezone.utc),
                            pull_request=None)
    fix_pr = SimpleNamespace(number=41, title="Fix add()",
                             html_url="https://example.test/pr/41",
                             head=SimpleNamespace(ref="overseer/fix-7-abc123"))
    other_pr = SimpleNamespace(number=40, title="unrelated",
                               html_url="https://example.test/pr/40",
                               head=SimpleNamespace(ref="feature/foo"))
    gh = FakeGithub(issues=[issue], open_prs=[other_pr, fix_pr])
    monkeypatch.setattr(o, "_github", lambda: gh)
    r = o.list_open_issues(repo)
    assert r["status"] == "ok"
    assert [i["number"] for i in r["open_issues"]] == [7]
    assert r["open_fix_prs"] == [{"number": 41, "branch": "overseer/fix-7-abc123",
                                  "title": "Fix add()",
                                  "url": "https://example.test/pr/41"}]


def test_comment_on_issue(fake_remote, monkeypatch):
    repo, _ = fake_remote
    gh = FakeGithub()
    monkeypatch.setattr(o, "_github", lambda: gh)
    r = o.comment_on_issue(repo, 12, "Needs an owner decision: A or B.")
    assert r["status"] == "commented"
    assert gh.comments == [(12, "Needs an owner decision: A or B.")]


def test_close_issue_comments_then_closes(fake_remote, monkeypatch):
    repo, _ = fake_remote
    gh = FakeGithub()
    monkeypatch.setattr(o, "_github", lambda: gh)
    r = o.close_issue(repo, 24, "Implemented in abc1234 (bot/metrics.py); verified in a fresh clone.")
    assert r["status"] == "closed"
    # The evidence comment lands before the close, and the close is 'completed'.
    assert gh.comments == [(24, "Implemented in abc1234 (bot/metrics.py); verified in a fresh clone.")]
    assert gh.edits == [(24, {"state": "closed", "state_reason": "completed"})]


def test_close_issue_refuses_unconfigured_repo():
    assert o.close_issue("evil/other-repo", 1, "why")["status"] == "error"


def test_close_issue_dry_run(fake_remote, capsys):
    repo, _ = fake_remote
    o.set_dry_run(True)
    try:
        assert o.close_issue(repo, 5, "done in abc123")["status"] == "dry_run"
    finally:
        o.set_dry_run(False)
    assert "[DRY-RUN]" in capsys.readouterr().out


def test_janitor_tool_subset_cannot_change_code():
    # The Janitor verifies and closes; it must never see the tools that could
    # modify a repo or open PRs.
    import agent_janitor
    names = {t["name"] for t in o.tool_specs(agent_janitor.TOOL_NAMES)}
    assert "close_issue" in names and "comment_on_issue" in names
    for forbidden in ("write_workspace_file", "commit_and_push",
                      "open_pull_request", "file_issue", "send_telegram_summary"):
        assert forbidden not in names


# ── dry run ──────────────────────────────────────────────────────────────


def test_dry_run_intercepts_fixer_mutations(fake_remote, capsys):
    # --dry-run must stop anything from reaching the remote: the commit stays
    # local, the push is skipped, and PR/comment are printed instead of sent.
    repo, origin = fake_remote
    ws = o.setup_fix_workspace(repo, 7)
    o.write_workspace_file(repo, "calc.py", FIXED_CALC)
    o.set_dry_run(True)
    try:
        assert o.commit_and_push(repo, "fix add()")["status"] == "dry_run"
        assert o.open_pull_request(repo, "Fix add()", "body", 7)["status"] == "dry_run"
        assert o.comment_on_issue(repo, 7, "findings")["status"] == "dry_run"
    finally:
        o.set_dry_run(False)
    assert ws["branch"] not in remote_branches(origin)
    assert capsys.readouterr().out.count("[DRY-RUN]") == 3


# ── agent wiring ─────────────────────────────────────────────────────────


def test_fixer_tools_all_registered():
    for name in agent_fixer.TOOL_NAMES:
        assert name in o.TOOL_FUNCTIONS and name in o.TOOL_SCHEMAS


def test_fixer_tool_subset_is_isolated():
    # The Fixer can work in a clone and talk to issues/PRs, but it must never
    # see file_issue, propose_enhancement, or send_telegram_summary.
    fixer = {t["name"] for t in o.tool_specs(agent_fixer.TOOL_NAMES)}
    assert "commit_and_push" in fixer and "open_pull_request" in fixer
    for forbidden in ("file_issue", "propose_enhancement", "send_telegram_summary"):
        assert forbidden not in fixer
