# Implementer — drop-in for a reviewed project

The overseer files issues in your project's repo. This workflow is what turns a
few of them into pull requests.

Copy `implement.yml` into `.github/workflows/` **in your project repo** (not in
the overseer), then:

1. Add an `ANTHROPIC_API_KEY` secret to that repo.
2. Edit the *"Set up the project"* step for your stack (Python shown; swap in
   `setup-node` + `npm ci`, or delete it if nothing is needed).
3. In the **overseer** repo, give `OVERSEER_GITHUB_TOKEN` **Actions: write** and
   **Issues: write** on this repo. Without Actions: write the dispatch is
   rejected and the overseer reports the hand-over as failed.

That's it. Each Monday the overseer picks up to `OVERSEER_IMPLEMENT_MAX` filed
issues that pass its gate (confirmed bugs and `effort:low` enhancements) and
fires an `overseer-implement` dispatch at whichever repo the issue lives in. This
workflow picks it up, implements the issue on a branch, runs your tests, and
opens a PR that says `Closes #N`.

## What it will not do

- **Merge.** A pull request is where it stops, always.
- **Open a PR on a red suite.** If it can't get your tests passing it comments on
  the issue with what blocked it and leaves the branch unpushed.
- **Work outside the issue.** The prompt forbids drive-by refactors and
  dependency bumps; keeping that true is the reason the diffs stay reviewable.

## Two things worth knowing

**Your PR checks won't run on these PRs.** A pull request opened with the
built-in `GITHUB_TOKEN` doesn't trigger further workflows — that's GitHub's loop
guard, not a bug here. It's why the agent runs your suite itself before opening
one. If you want your normal CI on them too, pass a PAT as `github_token` in the
action step instead.

**Opting an issue out.** Label it `overseer:no-implement` and the gate skips it
permanently. Issues already handed over carry `overseer:implementing`, so they're
never dispatched twice.

## Testing it before you trust it

From the overseer repo:

```bash
python scripts/dispatch_implement.py --dry-run --explain
```

That prints the exact queue it would hand over and the reason every other filed
issue was rejected, without firing anything. Then run this workflow by hand
(Actions → *Implement a filed issue* → Run workflow) against one issue number you
picked yourself, and read the PR it produces before wiring the schedule up.
