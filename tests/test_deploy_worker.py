"""Tests for the Worker's deploy path (overseer, 2026-09-06).

THE INCIDENT. worker/ was the one part of this system that main did not
control. The review's off-GitHub trigger was firing on Sunday because
wrangler.toml carried GitHub's day-of-week convention rather than Cloudflare's;
the fix merged the same afternoon and then failed to reach production twice —
once because nobody ran `wrangler deploy`, and once because it was run against a
checkout four days stale, which re-installed the broken schedule and printed
"Deployed overseer-ask triggers" while doing it.

For those days main said Monday and Cloudflare ran Sunday, with nothing
anywhere reporting the disagreement. It was caught by a human reading trigger
lines out of terminal output.

Two things follow, and these pin both: the deploy comes from main (so it cannot
be stale), and it asserts what Cloudflare installed against what wrangler.toml
declared (so a wrong schedule is red rather than quiet).
"""

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import verify_worker_triggers as vwt  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-worker.yml"
WRANGLER = ROOT / "worker" / "wrangler.toml"


def workflow_text():
    return WORKFLOW.read_text(encoding="utf-8")


# ── the deploy cannot come from a workspace ──────────────────────────────

def test_the_deploy_workflow_exists():
    assert WORKFLOW.exists(), (
        "without this, worker/ is deployed by a human remembering a command — "
        "which is how 2026-09-06 stayed broken through a merge and a deploy")


def test_it_deploys_on_a_push_to_main():
    # The whole point: GitHub checks out the pushed commit, so the tree being
    # deployed is main's by construction. A workspace can be four days old.
    import yaml
    triggers = yaml.safe_load(workflow_text())[True]
    assert "main" in triggers["push"]["branches"]


def test_it_watches_the_worker_directory():
    import yaml
    paths = yaml.safe_load(workflow_text())[True]["push"]["paths"]
    assert any(p.startswith("worker/") for p in paths), (
        "a change to the Worker must trigger its own deploy")
    assert any("verify_worker_triggers" in p for p in paths), (
        "a change to the verifier must re-run the verifier")


def test_a_missing_credential_fails_loudly():
    # wrangler's own message for an unset token reads like a broken workflow
    # rather than an unset secret. Say which secret, and where it goes.
    text = workflow_text()
    assert "CLOUDFLARE_API_TOKEN is not set" in text
    assert "Edit Cloudflare Workers" in text, "say which token template to use"


def test_the_token_is_a_secret_not_a_literal():
    text = workflow_text()
    assert "secrets.CLOUDFLARE_API_TOKEN" in text
    # A Cloudflare token is 40 URL-safe characters; a literal one here would be
    # committed and public.
    assert not re.search(r"CLOUDFLARE_API_TOKEN:\s*[A-Za-z0-9_-]{30,}", text)


def test_the_deploy_is_verified():
    text = workflow_text()
    assert "verify_worker_triggers.py" in text, (
        "a deploy that installs the wrong schedule must fail, not just print")
    # tee + pipefail: without pipefail a failing wrangler exits 0 through the
    # pipe and the verifier runs against a truncated log.
    assert "pipefail" in text, "a failing wrangler must not pass through the pipe"


# ── the verifier actually catches the thing it exists for ────────────────

GOOD_LOG = """\
Uploaded overseer-ask
Deployed overseer-ask triggers
  https://overseer-ask.andyrbrett.workers.dev
  schedule: 5 14 * * 2
  schedule: 5 17 * * 2
  schedule: 20 15 * * *
Current Version ID: 1085e66e-7de5-4b1c-8715-cc5cc18a98ca
"""

# The real output of the 2026-09-06 stale deploy: reports success, installs the
# schedule the merge was supposed to replace.
STALE_LOG = GOOD_LOG.replace("* * 2", "* * 1")


def test_it_passes_when_cloudflare_agrees_with_the_file():
    ok, message = vwt.compare(["5 14 * * 2", "5 17 * * 2", "20 15 * * *"],
                              vwt.installed_crons(GOOD_LOG))
    assert ok, message


def test_it_catches_the_stale_deploy():
    # The exact 09-06 failure, replayed. wrangler said "Deployed"; this must not.
    declared = ["5 14 * * 2", "5 17 * * 2", "20 15 * * *"]
    ok, message = vwt.compare(declared, vwt.installed_crons(STALE_LOG))
    assert not ok
    assert "5 14 * * 2" in message and "5 14 * * 1" in message, (
        "the message must show both schedules — otherwise it says something is "
        "wrong without saying what to compare")


def test_it_refuses_to_pass_on_an_empty_parse():
    # A verifier that passes when it cannot see anything shares a failure mode
    # with what it watches, which is the one thing an alarm may not do.
    ok, message = vwt.compare(["5 14 * * 2"], vwt.installed_crons("Uploaded\nDone\n"))
    assert not ok
    assert "no 'schedule:' lines" in message


def test_it_refuses_to_pass_when_the_file_declares_nothing():
    ok, _ = vwt.compare([], ["5 14 * * 2"])
    assert not ok


def test_order_is_not_treated_as_a_difference():
    # Cloudflare does not promise to echo them in file order.
    ok, _ = vwt.compare(["a", "b"], ["b", "a"])
    assert ok


def test_colour_codes_do_not_break_the_parse():
    # wrangler colours its output when it believes it has a terminal; a check
    # that only works on plain output is a check that stops working silently.
    coloured = "  \x1b[32mschedule:\x1b[0m 5 14 * * 2\n"
    assert vwt.installed_crons(coloured) == ["5 14 * * 2"]


def test_the_verifier_reads_the_real_wrangler_file():
    # Guards the seam between the script and the actual config: a renamed
    # [triggers] table would make the check silently find nothing to verify.
    assert vwt.declared_crons(WRANGLER), "no crons parsed out of worker/wrangler.toml"


@pytest.mark.parametrize("path", [".gitignore"])
def test_wranglers_local_cache_is_ignored(path):
    # worker/.wrangler/ is wrangler's build cache. Untracked and noisy, and the
    # kind of directory that eventually gets committed by a wildcard `git add`.
    assert ".wrangler" in (ROOT / path).read_text(encoding="utf-8")
