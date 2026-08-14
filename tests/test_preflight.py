"""Tests for the credential preflight (tools.preflight_github).

Regression cover for the 2026-07-20 → 08-10 outage: an expired PAT made every
tool call 401 while the run still reported success. Preflight's job is to catch
that before an agent starts, and to distinguish it from the subtler case of a
token that authenticates but was regenerated without one of the project repos.
"""

import pytest

import tools as o


class _FakeUser:
    login = "AndyRBrett"


class _FakeGithub:
    """Stand-in for the PyGithub client: `bad` is the set of repo slugs that
    should raise, mimicking a token not granted those repos."""

    def __init__(self, bad=(), auth_status=None):
        self.bad, self.auth_status = set(bad), auth_status

    def get_user(self):
        if self.auth_status:
            raise _GithubError(self.auth_status)
        return _FakeUser()

    def get_repo(self, slug):
        if slug in self.bad:
            raise _GithubError(404)
        return object()


class _GithubError(Exception):
    def __init__(self, status):
        super().__init__(f"{status} error")
        self.status = status


@pytest.fixture
def wired(monkeypatch):
    """Point the preflight at a known set of project repos and a fake client."""
    monkeypatch.setenv("OVERSEER_GITHUB_TOKEN", "ghp_fake")
    for key, slug in (("trading_bot", "A/crypto-trading"), ("volleyball", "A/coachvision"),
                      ("ufc", "A/ufc-dashboard"), ("overseer", "A/overseer")):
        monkeypatch.setitem(o.PROJECTS[key], "repo", slug)

    def install(client):
        monkeypatch.setattr(o, "_github", lambda: client)

    return install


def test_missing_token_is_not_configured_and_not_fatal(monkeypatch):
    # Local dev with no token: degrade gracefully, don't abort the pipeline.
    monkeypatch.delenv("OVERSEER_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = o.preflight_github()
    assert result["status"] == "not_configured"
    assert result["fatal"] is False


def test_expired_token_is_fatal(wired):
    # THE REGRESSION TEST: a 401 must stop the run, not degrade into a green
    # digest that reviewed nothing.
    wired(_FakeGithub(auth_status=401))
    result = o.preflight_github()
    assert result["status"] == "error"
    assert result["fatal"] is True
    assert "expired or revoked" in result["detail"]
    assert "OVERSEER_GITHUB_TOKEN" in result["detail"]


def test_healthy_token_reports_ok(wired):
    wired(_FakeGithub())
    result = o.preflight_github()
    assert result["status"] == "ok"
    assert result["fatal"] is False
    assert result["login"] == "AndyRBrett"
    assert set(result["repos"].values()) == {"ok"}


def test_one_missing_repo_warns_but_is_not_fatal(wired):
    # Token regenerated without the overseer repo — the classic omission, since
    # self-review is the one repo you don't think of as a "project".
    wired(_FakeGithub(bad=["A/overseer"]))
    result = o.preflight_github()
    assert result["status"] == "error"
    assert result["fatal"] is False, "one unreachable repo shouldn't kill the whole review"
    assert "A/overseer" in result["detail"]
    assert result["repos"]["A/crypto-trading"] == "ok"


def test_token_reaching_no_repos_is_fatal(wired):
    # Authenticates fine but was scoped to nothing useful — a review that can
    # read nothing is worth no more than one that never ran.
    wired(_FakeGithub(bad=["A/crypto-trading", "A/coachvision",
                           "A/ufc-dashboard", "A/overseer"]))
    result = o.preflight_github()
    assert result["status"] == "error"
    assert result["fatal"] is True
    assert "reaches none" in result["detail"]


def test_unconfigured_repos_are_skipped(wired, monkeypatch):
    # A project with no repo slug set isn't a failure, it's just not configured.
    monkeypatch.setitem(o.PROJECTS["ufc"], "repo", None)
    wired(_FakeGithub())
    result = o.preflight_github()
    assert result["status"] == "ok"
    assert "A/ufc-dashboard" not in result["repos"]
    assert len(result["repos"]) == 3
