"""Tests for the credential preflight (tools.preflight_github).

Regression cover for the 2026-07-20 → 08-10 outage: an expired PAT made every
tool call 401 while the run still reported success. Preflight's job is to catch
that before an agent starts, and to distinguish it from the subtler case of a
token that authenticates but was regenerated without one of the project repos.
"""

import time

import pytest

import tools as o


class _FakeUser:
    login = "AndyRBrett"


class _FakeGithub:
    """Stand-in for the PyGithub client: `bad` is the set of repo slugs that
    should raise, mimicking a token not granted those repos.

    `auth_error` raises on get_user for `auth_flaps` calls before succeeding —
    0 flaps and a set error means it never recovers. `repo_error` overrides what
    a `bad` repo raises, so an outage can be told from a scoping mistake.
    """

    def __init__(self, bad=(), auth_status=None, auth_error=None, auth_flaps=0,
                 repo_error=None):
        self.bad, self.auth_status = set(bad), auth_status
        self.auth_error, self.auth_flaps = auth_error, auth_flaps
        self.repo_error = repo_error
        self.user_calls = 0

    def get_user(self):
        self.user_calls += 1
        if self.auth_status:
            raise _GithubError(self.auth_status)
        if self.auth_error and (not self.auth_flaps or self.user_calls <= self.auth_flaps):
            raise self.auth_error
        return _FakeUser()

    def get_repo(self, slug):
        if slug in self.bad:
            raise self.repo_error or _GithubError(404)
        return object()


class _GithubError(Exception):
    def __init__(self, status):
        super().__init__(f"{status} error")
        self.status = status


def _outage():
    """The 2026-08-17 failure verbatim: requests' ConnectionError after urllib3
    burned its retries on 503s. Note the absence of any HTTP status."""
    return ConnectionError(
        "HTTPSConnectionPool(host='api.github.com', port=443): Max retries "
        "exceeded with url: /user (Caused by ResponseError('too many 503 "
        "error responses'))"
    )


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Retry backoff without the wall-clock cost."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


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


def test_transient_classification():
    # 5xx and rate limiting are GitHub having a moment; 401/403/404 are ours.
    assert o._is_transient(_outage()) is True
    assert o._is_transient(_GithubError(503)) is True
    assert o._is_transient(_GithubError(502)) is True
    assert o._is_transient(_GithubError(429)) is True
    assert o._is_transient(TimeoutError("timed out")) is True
    assert o._is_transient(_GithubError(401)) is False
    assert o._is_transient(_GithubError(404)) is False
    assert o._is_transient(RuntimeError("No GitHub token.")) is False


def test_brief_outage_is_retried_and_recovers(wired, monkeypatch):
    # The whole point of the retry: PyGithub gives up ~9s into a 503 wobble,
    # which is shorter than the wobble.
    monkeypatch.setattr(o, "PREFLIGHT_ATTEMPTS", 3)
    client = _FakeGithub(auth_error=_outage(), auth_flaps=2)
    wired(client)
    result = o.preflight_github()
    assert result["status"] == "ok"
    assert client.user_calls == 3


def test_sustained_outage_is_unavailable_not_a_credential_error(wired, monkeypatch):
    # THE REGRESSION TEST for 2026-08-17: every job failed on api.github.com
    # 503s and the log blamed "GitHub auth failed", pointing the on-call at a
    # token that was never the problem.
    monkeypatch.setattr(o, "PREFLIGHT_ATTEMPTS", 2)
    client = _FakeGithub(auth_error=_outage())
    wired(client)
    result = o.preflight_github()
    assert result["status"] == "unavailable"
    assert result["transient"] is True
    assert result["fatal"] is True, "a run that cannot read GitHub still can't proceed"
    assert client.user_calls == 2
    assert "expired or revoked" not in result["detail"]


def test_expired_token_is_not_retried(wired, monkeypatch):
    # A 401 will 401 again. Retrying it just delays the real diagnosis.
    monkeypatch.setattr(o, "PREFLIGHT_ATTEMPTS", 3)
    client = _FakeGithub(auth_status=401)
    wired(client)
    assert o.preflight_github()["fatal"] is True
    assert client.user_calls == 1


def test_outage_on_every_repo_is_not_reported_as_a_scoping_problem(wired):
    # Authenticated fine, then every repo read 503s. Same symptom as a token
    # scoped to nothing, opposite fix — so it must not read as the latter.
    wired(_FakeGithub(bad=["A/crypto-trading", "A/coachvision",
                           "A/ufc-dashboard", "A/overseer"],
                      repo_error=_GithubError(503)))
    result = o.preflight_github()
    assert result["status"] == "unavailable"
    assert result["transient"] is True
    assert "outage" in result["detail"]


def test_one_repo_down_still_warns_by_name(wired):
    # A partial outage is indistinguishable from a partial scope and both are
    # non-fatal, so the existing warning path is the right one.
    result_client = _FakeGithub(bad=["A/overseer"], repo_error=_GithubError(503))
    wired(result_client)
    result = o.preflight_github()
    assert result["fatal"] is False
    assert "A/overseer" in result["detail"]


def test_unconfigured_repos_are_skipped(wired, monkeypatch):
    # A project with no repo slug set isn't a failure, it's just not configured.
    monkeypatch.setitem(o.PROJECTS["ufc"], "repo", None)
    wired(_FakeGithub())
    result = o.preflight_github()
    assert result["status"] == "ok"
    assert "A/ufc-dashboard" not in result["repos"]
    assert len(result["repos"]) == 3
