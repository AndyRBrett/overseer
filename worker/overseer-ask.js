/**
 * The voice front door. Deliberately the dumbest part of this project.
 *
 * It fetches the context pack that `scripts/build_ask_context.py` publishes to
 * GitHub Pages, concatenates four strings, makes one API call, and returns
 * plain text. It does not know what the implementation gate is, why an issue is
 * queued, or what any field in the pack means.
 *
 * That is the entire design. Invariant 4 exists because the dashboard once
 * re-derived the gate's rules in app.js and confidently described a queue the
 * dispatcher would never produce. A Worker is that same hazard one platform
 * further away — deployed separately, testable by nobody, and out of sight when
 * the Python changes. So everything that could drift lives in the pack, and
 * `tests/test_ask.py` greps this file to make sure it stays that way.
 *
 * It has one other job, added 2026-08-31 and deliberately kept at arm's length
 * from the above: a cron that pokes GitHub into running the weekly review. See
 * fireWeeklyReview() for why that lives here rather than in a workflow.
 *
 * Deploy:
 *   npx wrangler secret put ANTHROPIC_API_KEY
 *   npx wrangler secret put ASK_SHARED_SECRET
 *   npx wrangler secret put DISPATCH_TOKEN
 *   npx wrangler deploy
 */

const API_URL = "https://api.anthropic.com/v1/messages";

// Must match the repository_dispatch `types:` in .github/workflows/weekly-review.yml.
// A typo here is a dispatch GitHub accepts (204) and no workflow reacts to, so a
// test pins the two together.
const DISPATCH_EVENT = "weekly-review";
const DEFAULT_MODEL = "claude-sonnet-5";

// Voice answers are two or three sentences. Nothing here needs room to ramble,
// and output tokens are the expensive half of a question.
const MAX_TOKENS = 1024;

// The pack only changes when the ledger refreshes (hourly at :20) or the weekly
// review publishes, so re-fetching it per question buys nothing and adds a
// round trip to a request a human is waiting on out loud.
const PACK_TTL_SECONDS = 300;

/** Constant-time-ish compare, so a wrong secret can't be found a byte at a time. */
function secretMatches(given, expected) {
  if (!given || !expected || given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < given.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

/**
 * Append the real reason when the caller asked for it.
 *
 * Safe to return: this is only reachable past the shared-secret check, so the
 * only person who ever sees it is the one who owns the key that failed. The
 * default stays a plain sentence because the usual caller is a phone reading
 * the answer out loud, and "HTTP 401 authentication_error" spoken aloud helps
 * nobody.
 */
function explain(message, err, debug) {
  return debug ? `${message}\n\n${String(err)}` : message;
}

/** Plain text, because the caller is a phone that is about to read this aloud. */
function say(text, status = 200) {
  return new Response(text + "\n", {
    status,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

/**
 * The one piece of logic shared with Python, and the only place the two could
 * drift. Mirrors ask_context.system_for exactly: prompt, format rule, then the
 * facts as the bytes Python already serialized. Never JSON.stringify the facts
 * here — JavaScript renders JSON differently enough (key order, separators,
 * non-ASCII escaping) to miss the prompt cache on every single question while
 * looking completely correct.
 */
function systemFor(pack, format) {
  const rule = (pack.formats || {})[format] || (pack.formats || {}).voice || "";
  return `${pack.system}\n\n${rule}\n\nFACTS:\n${pack.facts_json}`;
}

/**
 * Mirrors ask_context.user_turn. The clock goes in the user turn, never in the
 * system block: anything volatile inside the cached prefix invalidates it, so a
 * timestamp one line higher would quietly pay full input price for a 3.5k-token
 * pack on every single question.
 */
function userTurn(question) {
  const now = new Date().toISOString().replace(/\.\d+Z$/, "Z");
  return `[current time: ${now}]\n${question}`;
}

/**
 * Poke GitHub into running the weekly review.
 *
 * WHY THIS IS IN A CLOUDFLARE WORKER. On 2026-08-31 GitHub delivered none of
 * the weekly review's three scheduled events — 14:00, 16:00 and 18:00 UTC all
 * passed with no workflow run created at all. Not a failure: no job, no log,
 * nothing red, so none of the workflow's own hardening (in-job retries, the
 * catch-up crons) could engage, and nothing alerted. Scheduled workflows on
 * free public repos are deprioritised hard enough that ledger-refresh's HOURLY
 * cron managed two firings in eighteen hours that day.
 *
 * The catch-up crons could not fix this because they share the scheduler that
 * dropped the primary. Redundancy has to come from a different vendor, so it
 * comes from here: this Worker already exists, already deploys, and its cron
 * runs on Cloudflare's scheduler rather than GitHub's. repository_dispatch was
 * the natural target because ledger-refresh already exposes one and the
 * event-driven paths were the ones that kept working on 08-31.
 *
 * This does NOT decide whether the review should run — scripts/weekly_guard.py
 * does, on the GitHub side, for every automated trigger. Keeping that judgement
 * out of here is invariant 8: a rule that lives in the Worker is deployed
 * separately and drifts out of sight of the Python. All this knows is "it is
 * Monday, ask".
 */
async function fireWeeklyReview(env) {
  const repo = env.DISPATCH_REPO;
  if (!env.DISPATCH_TOKEN || !repo) {
    // Loud, because the symptom otherwise is a review that silently stops
    // happening — which is the exact failure this was built to end.
    console.error("[dispatch] not configured: need DISPATCH_TOKEN and DISPATCH_REPO");
    return;
  }

  try {
    const res = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.DISPATCH_TOKEN}`,
        accept: "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        // GitHub rejects an API request with no User-Agent outright, and the
        // 403 it returns reads like a permissions problem rather than a
        // missing header.
        "user-agent": "overseer-weekly-trigger",
        "content-type": "application/json",
      },
      body: JSON.stringify({ event_type: DISPATCH_EVENT }),
    });
    // 204 No Content is success here; anything else has a body worth reading.
    if (res.status !== 204) {
      throw new Error(`dispatch ${res.status}: ${(await res.text()).slice(0, 200)}`);
    }
    console.log(`[dispatch] asked ${repo} to run the weekly review`);
  } catch (err) {
    // Nothing to retry into — the next Cloudflare cron is the retry, and the
    // guard makes a duplicate harmless. Log it so `wrangler tail` can say why a
    // Monday was quiet.
    console.error("[dispatch] failed:", String(err));
  }
}

export default {
  // Cloudflare's scheduler, not GitHub's — that separation is the whole point.
  // waitUntil so the POST is not cut off when the handler returns.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(fireWeeklyReview(env));
  },

  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "access-control-allow-origin": env.ALLOWED_ORIGIN || "*",
          "access-control-allow-headers": "authorization,content-type",
          "access-control-allow-methods": "POST,OPTIONS",
        },
      });
    }

    const auth = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
    if (!secretMatches(auth, env.ASK_SHARED_SECRET)) {
      // A bot token or endpoint that leaks is somebody else spending your API
      // key, so this is the first check and it is never skipped in dev.
      return say("Not authorised.", 401);
    }

    const url = new URL(request.url);
    let question = url.searchParams.get("q") || "";
    let format = url.searchParams.get("format") || "voice";
    let rawDebug = false;
    if (request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      question = body.q || body.question || question;
      format = body.format || format;
      rawDebug = body.debug === true;
    }
    const debug = url.searchParams.get("debug") === "1" || rawDebug === true;
    question = String(question).trim();
    if (!question) return say("Ask me something.", 400);
    // A question is a sentence. Anything longer is a paste, an accident, or
    // someone trying to make your key summarise their document.
    if (question.length > 500) return say("That question is too long.", 400);

    let pack;
    try {
      const packUrl = env.PACK_URL;
      const res = await fetch(packUrl, { cf: { cacheTtl: PACK_TTL_SECONDS, cacheEverything: true } });
      if (!res.ok) throw new Error(`pack fetch ${res.status}`);
      pack = await res.json();
    } catch (err) {
      console.error("[ask] pack fetch failed:", env.PACK_URL, String(err));
      return say(explain("I can't reach my notes right now, so I'd only be guessing.",
                         err, debug), 502);
    }

    const payload = {
      model: env.ASK_MODEL || pack.model_hint || DEFAULT_MODEL,
      max_tokens: MAX_TOKENS,
      // No thinking and no tools: the answer is a lookup in a fact block Python
      // already computed. A tool loop is what makes the pipeline's agents cost
      // dollars instead of fractions of a cent.
      thinking: { type: "disabled" },
      system: [{ type: "text", text: systemFor(pack, format),
                 cache_control: { type: "ephemeral" } }],
      messages: [{ role: "user", content: userTurn(question) }],
    };

    let data;
    try {
      const res = await fetch(API_URL, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": env.ANTHROPIC_API_KEY,
          "anthropic-version": "2023-06-01",
        },
        body: JSON.stringify(payload),
      });
      data = await res.json();
      if (!res.ok) throw new Error(data?.error?.message || `api ${res.status}`);
    } catch (err) {
      // Spoken aloud, so the default is a sentence rather than a stack trace.
      // But a failure you cannot see the reason for is a failure you cannot
      // fix: the real error goes to the log (`wrangler tail`) every time, and
      // comes back in the response when the caller explicitly asks for it.
      console.error("[ask] model call failed:", String(err));
      return say(explain("Something went wrong reaching the model. Try again in a moment.",
                         err, debug), 502);
    }

    const answer = (data.content || [])
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join("\n")
      .trim();

    return say(answer || "I don't have an answer for that.");
  },
};
