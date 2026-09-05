"""The whole weekly pipeline, end to end, against fixtures (overseer #50).

The unit suite proves the parts. This proves the seams — tracer → digest writer
→ dashboard JSON, ledger → agent prompts, run_agent stitching its deterministic
banners onto a digest before it is sent. Every one of those has two correct
sides and no test of its own, and the thing that notices when they disagree is
Monday's run, an hour and a third of a dollar in (#32).

Run in a SUBPROCESS, deliberately. The harness sets the repo environment before
importing `tools` — PROJECTS and the output paths are built at import time — so
running it in-process would either read a `tools` some earlier test already
imported, or leave a `tools` configured for a fixture world behind for every
test after it. A fresh interpreter is also exactly what CI runs.
"""

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pipeline_dryrun.py"


@pytest.fixture(scope="module")
def run_artifacts(tmp_path_factory):
    out = tmp_path_factory.mktemp("e2e")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--keep", str(out)],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    assert proc.returncode == 0, f"the dry run failed:\n{proc.stdout}\n{proc.stderr}"
    return out, proc


def _json(out, name):
    return json.loads((out / name).read_text(encoding="utf-8"))


def test_the_pipeline_completes_and_says_so(run_artifacts):
    out, proc = run_artifacts
    assert "[e2e] OK" in proc.stdout
    assert _json(out, "digest.json")["status"] == "completed"


def test_every_artifact_the_dashboard_reads_is_written(run_artifacts):
    out, _ = run_artifacts
    for name in ("digest.json", "history.json", "shipped.json"):
        assert (out / name).exists(), f"{name} was not written"


def test_the_digest_carries_every_deterministic_block(run_artifacts):
    # Invariant 7. Each of these is computed in Python and stitched in by
    # run_agent precisely because a section an agent has to remember is a section
    # that eventually goes quiet with nothing failing.
    summary = _json(run_artifacts[0], "digest.json")["summary"]
    for heading in ("STALENESS ALERTS", "ATTENTION RANKING",
                    "IMPLEMENTED", "AGING BACKLOG"):
        assert heading in summary, f"the digest lost its {heading} block"


def test_the_run_reads_every_project_exactly_once(run_artifacts):
    out, _ = run_artifacts
    digest = _json(out, "digest.json")
    assert len(digest["projects"]) == 4
    # The four shared reads are recorded against the pseudo-agent "Telemetry".
    # An agent may still re-read a feed to confirm a bug — the Bug-Hunter does
    # exactly that in the script — so the count that matters is the shared one.
    shared = [t for t in digest["timeline"]
              if t["label"].startswith("read_") and t["agent"] == "Telemetry"]
    assert len(shared) == 4, "the shared telemetry read did not cover all four projects"


def test_the_ledger_the_dashboard_reads_carries_the_queue_and_the_yield(run_artifacts):
    ledger = _json(run_artifacts[0], "shipped.json")
    assert ledger["entries"] and ledger["totals"]["proposed"]
    assert ledger["queue"]["next"], "the implementation gate picked nothing to do"
    assert ledger["outcomes"]["by_repo"], "no per-repo proposal outcomes (#60)"


def test_a_duplicate_proposal_is_blocked_and_the_count_says_so(run_artifacts):
    # The scripted agent proposes a reworded copy of an issue already on file.
    counts = _json(run_artifacts[0], "digest.json")["counts"]
    assert counts["duplicates_blocked"] == 1
    # And the refused filing is not counted as an idea: a call that returned is
    # not a call that filed.
    assert counts["enhancements"] == 1


def test_the_failure_path_is_loud(tmp_path):
    # The 2026-07 credential outage reported SUCCESS for four weeks. A harness
    # that can pass on a broken world is worth nothing, so pin that a corrupted
    # fixture fails the job rather than producing a green empty run.
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"login": "x", "repos": {}}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--fixtures", str(broken)],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    assert proc.returncode == 1
    # Two shapes of loud: the harness's own assertion, or the pipeline refusing
    # to review a world it cannot read. Either is a red job; a green one is not
    # on the menu.
    assert "FAILED" in proc.stderr or "ABORTED" in proc.stderr


def test_ci_runs_this_as_its_own_job():
    import yaml
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "e2e" in jobs, "the end-to-end job is not wired into CI"
    steps = " ".join(str(s.get("run", "")) for s in jobs["e2e"]["steps"])
    assert "scripts/pipeline_dryrun.py" in steps
    # On pull requests, or it cannot catch anything before it merges.
    assert "pull_request" in workflow[True] or "pull_request" in workflow.get("on", {})
