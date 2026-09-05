"""
Run the ENTIRE weekly pipeline end to end against fixtures (overseer #50).

WHY A WHOLE-PIPELINE TEST EXISTS ON TOP OF 350 UNIT TESTS. Every component here
is tested, and the weekly review has still failed in production on the joins
between them: the tracer feeding the digest writer, the digest writer feeding
the dashboard's JSON, the ledger feeding the agents' prompts, `run_agent`
stitching deterministic banners onto a digest before it is sent. Those seams have
no unit — each side is correct and the two disagree about a key name, and the
first thing that notices is Monday's run, an hour and a third of a dollar in.

So this runs `orchestrator.run_pipeline` itself. Not a re-implementation of it,
not a subset — the real entrypoint, the real tool dispatch, the real tracer, the
real digest writers. Two things are replaced and nothing else:

  * GitHub — a fake client serving the fixture world, installed by assigning
    `tools._gh`. That is the module's own cache; every read and write in the
    pipeline goes through `_github()` and gets the fake, including the ones this
    file has never heard of.
  * Anthropic — a scripted client that returns tool calls instead of thinking.
    It is a stand-in for the model, NOT for the tools: every tool call it makes
    is executed for real against the fake GitHub, which is where the value is.

No network, no key, no model spend, and it fails loudly rather than reporting
success like the 2026-07 credential outage did.

    python scripts/pipeline_dryrun.py            # run, assert, clean up
    python scripts/pipeline_dryrun.py --keep DIR # leave the artifacts to inspect

Exit code 0 when the pipeline produced a complete, well-formed set of artifacts;
1 with a named assertion when it did not.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "pipeline", "world.json")

# The fixture world's repos, in the env vars the real deployment uses. Set before
# `tools` is imported, because PROJECTS is built at import time from these.
FIXTURE_ENV = {
    "OVERSEER_GITHUB_TOKEN": "fixture-token",
    "ANTHROPIC_API_KEY": "fixture-key",
    "TRADING_REPO": "fixture/crypto-trading",
    "VOLLEYBALL_REPO": "fixture/coachvision",
    "UFC_REPO": "fixture/ufc-dashboard",
    "OVERSEER_REPO": "fixture/overseer",
    # Local status-file paths would take precedence over the repo reads; make
    # sure an inherited environment cannot quietly change what this exercises.
    "TRADING_DB_PATH": "",
    "VOLLEYBALL_RESULTS_PATH": "",
    # Telegram stays unconfigured on purpose: send_telegram_summary then reports
    # not_configured without opening a socket, and the digest is still captured.
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
}

_RELATIVE = re.compile(r"^-(\d+(?:\.\d+)?)h$")


def _resolve(value, now):
    """Fixture timestamps are relative ('-6h'); make them real, recursively.

    Relative because a fixture dated in the past is a fixture that reads as
    catastrophically stale a month later, and every freshness rule in this
    pipeline is measured against the clock.
    """
    if isinstance(value, str):
        match = _RELATIVE.match(value)
        if match:
            return (now - timedelta(hours=float(match.group(1)))
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
        return value
    if isinstance(value, dict):
        return {k: _resolve(v, now) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, now) for v in value]
    return value


def _dt(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


# ── THE FAKE GITHUB ──────────────────────────────────────────────────────
# Only the surface the pipeline actually touches. Anything it grows a
# dependency on shows up here as an AttributeError with the missing method's
# name on it, which is a better failure than a mock that answers everything.


class _Label:
    def __init__(self, name):
        self.name = name


class _Content:
    def __init__(self, data):
        self.decoded_content = json.dumps(data).encode("utf-8")


class _Run:
    def __init__(self, spec, index):
        self.id = index
        self.name = spec.get("name", "workflow")
        self.status = spec.get("status", "completed")
        self.conclusion = spec.get("conclusion", "success")
        self.created_at = _dt(spec["created_at"])
        self.html_url = f"https://github.com/runs/{index}"


class _Workflow:
    def __init__(self, runs):
        self._runs = runs

    def get_runs(self):
        return self._runs


class _Issue:
    def __init__(self, repo, spec):
        self.repository = repo
        self.number = spec["number"]
        self.title = spec["title"]
        # The marker is what makes an issue OURS. Enhancements are recognised by
        # their title prefix; a filed bug is only recognised by this line, so the
        # fixture carries it exactly as file_issue writes it.
        self.body = spec.get("body", "")
        if spec.get("body_marker"):
            self.body = f"{self.body}\n\n---\n_Filed by Project Overseer._"
        self.labels = [_Label(name) for name in spec.get("labels", [])]
        self.state = spec.get("state", "open")
        self.state_reason = spec.get("state_reason")
        self.html_url = f"https://github.com/{repo.full_name}/issues/{self.number}"
        self.created_at = _dt(spec["created_at"]) if spec.get("created_at") else None
        self.closed_at = _dt(spec["closed_at"]) if spec.get("closed_at") else None
        # Everything the pipeline files arrives as OWNER — the association is
        # GitHub's own answer and the ledger's security boundary (see
        # tools._ledger_entry). A fixture that got this wrong would silently
        # produce an empty ledger and a green run.
        self.author_association = spec.get("author_association", "OWNER")
        self.pull_request = None

    def get_timeline(self):
        return []          # no linked PRs in the fixture world

    def add_to_labels(self, *names):
        self.labels.extend(_Label(n) for n in names)


class _Repo:
    def __init__(self, full_name, spec, now):
        self.full_name = full_name
        self._status = spec.get("status_file")
        self._runs = [_Run(r, i) for i, r in enumerate(spec.get("workflow_runs", []))]
        self.issues = [_Issue(self, s) for s in spec.get("issues", [])]
        self.dispatches = []
        self._next_number = max([i.number for i in self.issues] + [100]) + 1
        self._now = now

    def get_contents(self, path):
        if self._status is None:
            raise FileNotFoundError(f"404: no {path} in {self.full_name}")
        return _Content(self._status)

    def get_issues(self, state="all"):
        return list(self.issues)

    def get_issue(self, number):
        return next(i for i in self.issues if i.number == number)

    def create_issue(self, title, body):
        issue = _Issue(self, {"number": self._next_number, "title": title,
                              "body": body,
                              "created_at": self._now.strftime("%Y-%m-%dT%H:%M:%SZ")})
        self._next_number += 1
        self.issues.append(issue)
        return issue

    def get_workflow(self, workflow_file):
        return _Workflow(self._runs)

    def get_workflow_runs(self):
        return self._runs

    def create_repository_dispatch(self, event_type, client_payload):
        self.dispatches.append((event_type, client_payload))


class FakeGitHub:
    def __init__(self, world, now):
        self.repos = {name: _Repo(name, spec, now)
                      for name, spec in world["repos"].items()}
        self._login = world.get("login", "overseer-bot")

    def get_user(self):
        return types.SimpleNamespace(login=self._login)

    def get_repo(self, slug):
        if slug not in self.repos:
            raise LookupError(f"404: {slug} not found")
        return self.repos[slug]


# ── THE SCRIPTED MODEL ───────────────────────────────────────────────────


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _text(body):
    return _Block(type="text", text=body)


def _tool(name, tool_input, tool_id):
    return _Block(type="tool_use", name=name, input=dict(tool_input), id=tool_id)


class _Response:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        # Real numbers in the right shape: the spend panel and the tiering
        # baseline are computed from these, and a None here would have the
        # digest report $0.00 for a run that did work.
        self.usage = types.SimpleNamespace(
            input_tokens=1200, output_tokens=300,
            cache_creation_input_tokens=0, cache_read_input_tokens=0)


class ScriptedAnthropic:
    """Stands in for the model — one scripted turn list per agent.

    The agent is identified from its own system prompt rather than from a call
    counter, so the script does not silently shift by one when an agent takes an
    extra turn.
    """

    def __init__(self, script):
        self.script = script
        self.calls = []
        self._turns = {agent: 0 for agent in script}
        self.messages = self

    @staticmethod
    def _agent(system):
        # Matched on the prompt's own "You are the X" line, not on a bare name.
        # The Reviewer's prompt describes its inputs as "the BUG-HUNTER's
        # summary", so a substring search identified it as the Bug-Hunter and
        # replayed the wrong script at it.
        for marker, agent in (("You are the BUG-HUNTER", "Bug-Hunter"),
                              ("You are the IDEA AGENT", "Idea-Agent"),
                              ("You are the REVIEWER", "Reviewer")):
            if marker in system:
                return agent
        raise AssertionError("unrecognised agent system prompt")

    def create(self, *, model, system, tools=None, messages=None, **kw):
        agent = self._agent(system)
        turn = self._turns[agent]
        self._turns[agent] += 1
        self.calls.append({"agent": agent, "model": model, "turn": turn,
                           "system": system, "tools": [t["name"] for t in (tools or [])]})
        blocks = self.script[agent][min(turn, len(self.script[agent]) - 1)]
        content = [b(agent, turn) if callable(b) else b for b in blocks]
        stop = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
        return _Response(content, stop)


def build_script():
    """What each agent does this run.

    Chosen to walk the paths that break in production rather than the shortest
    ones: a bug filed against a real repo, a duplicate proposal that the gate
    must REFUSE, a clear one it must accept, and a digest that `run_agent` has to
    stitch four deterministic blocks onto.
    """
    return {
        "Bug-Hunter": [
            [_tool("read_volleyball_results", {}, "bh-1")],
            [_tool("file_issue", {
                "repo": "fixture/coachvision",
                "title": "overseer-status.json has not been republished in over two weeks",
                "body": "The published status file is ~400h old against a 192h SLA.",
            }, "bh-2")],
            [_text("BUGS FILED\n- coachvision: status feed has stopped publishing.")],
        ],
        "Idea-Agent": [
            # An idea that is already on file, worded differently. The dedupe
            # check must catch it (#33) — and so must propose_enhancement, which
            # is the half that does not depend on the agent asking first.
            [_tool("check_duplicate", {
                "repo": "fixture/overseer",
                "title": "Publish an aging backlog section in the weekly digest",
                "rationale": "Show how long open ideas have sat untriaged.",
            }, "ia-1")],
            [_tool("propose_enhancement", {
                "repo": "fixture/overseer",
                "title": "Publish an aging backlog section in the weekly digest",
                "rationale": "Show how long open ideas have sat untriaged.",
                "effort": "low", "impact": "medium",
            }, "ia-2")],
            [_tool("propose_enhancement", {
                "repo": "fixture/crypto-trading",
                "title": "Alert when the 90-day Sharpe ratio turns negative",
                "rationale": "The bot keeps trading through a negative-Sharpe regime.",
                "effort": "low", "impact": "high",
            }, "ia-3")],
            [_text("IDEAS\n- crypto-trading — Alert on negative 90d Sharpe "
                   "(effort: low, impact: high): the bot trades through it today.")],
        ],
        "Reviewer": [
            [_tool("send_telegram_summary", {
                "text": "WEEKLY REVIEW\n- coachvision's status feed has stopped.\n"
                        "- One idea filed for crypto-trading.",
            }, "rv-1")],
            [_text("Digest sent.")],
        ],
    }


# ── ASSERTIONS ───────────────────────────────────────────────────────────
# What "the pipeline works" means, stated once. Each check names the production
# failure it stands in for, because an assertion whose purpose nobody remembers
# is the one that gets deleted when it goes red.


class CheckFailed(Exception):
    pass


def _require(condition, message):
    if not condition:
        raise CheckFailed(message)


def check_artifacts(out_dir, github, client):
    digest = json.load(open(os.path.join(out_dir, "digest.json"), encoding="utf-8"))
    history = json.load(open(os.path.join(out_dir, "history.json"), encoding="utf-8"))
    ledger = json.load(open(os.path.join(out_dir, "shipped.json"), encoding="utf-8"))

    # The run itself.
    _require(digest["status"] == "completed",
             f"the pipeline did not complete: {digest['status']}")
    _require({c["agent"] for c in client.calls} == {"Bug-Hunter", "Idea-Agent", "Reviewer"},
             "not all three agents ran")

    # Reads → project health → digest. The four-week credential outage produced
    # a digest with every project blind and a green workflow.
    projects = digest["projects"]
    _require(len(projects) == 4, f"expected four projects, got {sorted(projects)}")
    _require(not [n for n, p in projects.items() if p["status"] in ("blind", "error")],
             f"a project could not be read: {projects}")
    _require(any(p["status"] == "stale" for p in projects.values()),
             "the 400h-old coachvision feed did not read as stale")

    # The attention ranking (#25) — computed, ordered, and explained.
    ranked = digest["attention"]
    _require(len(ranked) == 4, "the attention ranking is missing projects")
    _require(all(r["why"] for r in ranked), "an attention row has no reason")
    _require([r["score"] for r in ranked] == sorted((r["score"] for r in ranked),
                                                    reverse=True),
             "the attention ranking is not sorted by descending score")
    _require(ranked[0]["score"] > 0, "nothing scored, in a world with a dead feed")

    # The deterministic digest blocks — the ones that go quiet when an agent
    # forgets them, which is why they are not the agent's job (invariant 7).
    summary = digest["summary"]
    for heading in ("STALENESS ALERTS", "ATTENTION RANKING"):
        _require(heading in summary, f"the digest is missing its {heading} block")
    _require("WEEKLY REVIEW" in summary, "the Reviewer's own text was dropped")

    # The dedupe gate (#33): the re-worded proposal must have been refused, and
    # the genuinely new one filed.
    # Checked by NUMBER, not by title: the fixture already contains an issue with
    # that exact title — that is the whole point of the case — so a title search
    # can never tell a refusal from a re-filing. Created issues get fresh numbers
    # above 100.
    refiled = [i.number for i in github.repos["fixture/overseer"].issues if i.number > 100]
    _require(not refiled, f"a near-duplicate proposal was filed anyway: #{refiled}")
    crypto = {i.title for i in github.repos["fixture/crypto-trading"].issues}
    _require(any("Sharpe" in t for t in crypto), "the new proposal was not filed")

    # The bug the Bug-Hunter filed, stamped so the ledger can claim it later.
    coach = github.repos["fixture/coachvision"].issues
    filed = [i for i in coach if "republished" in i.title]
    _require(filed, "the confirmed bug was never filed")
    _require("_Filed by Project Overseer._" in filed[0].body,
             "the filed bug carries no overseer marker — the ledger will disown it")

    # Ledger, gate and yield, as the dashboard reads them.
    _require(ledger["entries"], "the published ledger is empty")
    _require(ledger["queue"]["next"], "the implementation queue picked nothing")
    outcomes = ledger["outcomes"]["by_repo"]
    _require(outcomes, "no per-repo proposal outcomes were published (#60)")
    _require(all("ship_rate" in row for row in outcomes.values()),
             "an outcome row has no ship_rate")

    # History, which the trend sparklines read.
    _require(history["runs"], "no history record was written")
    _require(history["runs"][-1]["projects"], "the history record has no project scores")

    return {"projects": len(projects), "entries": len(ledger["entries"]),
            "queued": len(ledger["queue"]["next"]),
            "model_calls": len(client.calls)}


# ── THE RUN ──────────────────────────────────────────────────────────────


def run(out_dir, fixtures=FIXTURES):
    """Run the pipeline against the fixture world, writing artifacts to out_dir."""
    now = datetime.now(timezone.utc)
    with open(fixtures, encoding="utf-8") as f:
        world = _resolve(json.load(f), now)

    os.environ.update(FIXTURE_ENV)
    os.environ["DIGEST_PATH"] = os.path.join(out_dir, "digest.json")
    os.environ["HISTORY_PATH"] = os.path.join(out_dir, "history.json")
    os.environ["LEDGER_PATH"] = os.path.join(out_dir, "shipped.json")

    # Imported AFTER the environment is set: PROJECTS and the output paths are
    # read at import time.
    sys.path.insert(0, ROOT)
    import tools  # noqa: E402
    import orchestrator  # noqa: E402

    github = FakeGitHub(world, now)
    tools._gh = github          # the module's own client cache — every read uses it

    client = ScriptedAnthropic(build_script())
    fake_sdk = types.ModuleType("anthropic")
    fake_sdk.Anthropic = lambda **kw: client
    sys.modules["anthropic"] = fake_sdk

    # The tracer writes its own two files next to the artifacts rather than into
    # the repo root.
    os.chdir(out_dir)
    orchestrator.run_pipeline()
    return github, client


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--keep", metavar="DIR",
                        help="write the artifacts here and leave them in place")
    parser.add_argument("--fixtures", default=FIXTURES)
    args = parser.parse_args(argv)

    out_dir = os.path.abspath(args.keep or tempfile.mkdtemp(prefix="overseer-e2e-"))
    os.makedirs(out_dir, exist_ok=True)
    cwd = os.getcwd()
    try:
        github, client = run(out_dir, args.fixtures)
        stats = check_artifacts(out_dir, github, client)
    except CheckFailed as exc:
        # A failure KEEPS the artifacts, whatever --keep says. The digest and the
        # ledger it wrote are the entire diagnosis, and a run that tidied them
        # away would leave one line of assertion text to debug an integration
        # failure from.
        print(f"\n[e2e] FAILED: {exc}", file=sys.stderr)
        print(f"[e2e] artifacts left in {out_dir}", file=sys.stderr)
        return 1
    finally:
        # run() chdirs into the output directory so the tracer's own two files
        # land there; get out before anything is removed.
        os.chdir(cwd)
    if not args.keep:
        shutil.rmtree(out_dir, ignore_errors=True)

    print(f"\n[e2e] OK — {stats['model_calls']} model turns, "
          f"{stats['projects']} projects read, {stats['entries']} ledger entries, "
          f"{stats['queued']} queued for implementation.")
    if args.keep:
        print(f"[e2e] artifacts in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
