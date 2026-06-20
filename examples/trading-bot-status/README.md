# Making the trading bot observable to the overseer (cloud, option B)

The overseer runs in the cloud and can't see the bot's database. So the bot
publishes a tiny `overseer-status.json` to its own repo each run, and the
overseer reads it via the GitHub API.

## Steps (in the `crypto-trading` repo)

1. **Copy `write_status.py`** into the repo root.
2. **Adapt `collect_metrics()`** to your actual trade store (the template
   assumes a `trades(ts, pnl)` SQLite table).
3. **Publish it each daily run** — add this step to the bot's existing daily
   workflow (it needs `permissions: contents: write`):

   ```yaml
         - name: Publish overseer status
           run: |
             python write_status.py
             if ! git diff --quiet -- overseer-status.json; then
               git config user.name  "trading-bot"
               git config user.email "trading-bot@users.noreply.github.com"
               git add overseer-status.json
               git commit -m "overseer status $(date -u +%Y-%m-%dT%H:%MZ)"
               git push
             fi
   ```

That's it. The overseer's `read_trading_bot_log` automatically reads
`overseer-status.json` from `TRADING_REPO` whenever `TRADING_DB_PATH` is unset
(the cloud case), flags it **stale** if `generated_at` is older than 48h, and
falls back to a clear error if the file isn't there yet.

> ⚠️ **Don't regenerate `overseer-status.json` locally / in a dev shell where the
> real data store isn't present.** Running `write_status.py` with no trade DB
> writes an empty/error payload — committing that overwrites the real metrics and
> the next overseer review reads it as a regression (Trading flips to IDLE/error).
> Let the scheduled workflow regenerate it against the real stores; only commit a
> hand-run file if it ran against live data.

## Overseer config (already set, for reference)

- `TRADING_REPO` = `AndyRBrett/crypto-trading` (variable) — where to read from
- `TRADING_STATUS_PATH` = `overseer-status.json` (optional variable; this is the default)
- `OVERSEER_GITHUB_TOKEN` must be able to read the repo (it already can, since it
  files issues there)

The same pattern works for the volleyball pipeline (`volleyball #2`) — publish a
results JSON and point a reader at it.
