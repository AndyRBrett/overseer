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
 * Deploy:
 *   npx wrangler secret put ANTHROPIC_API_KEY
 *   npx wrangler secret put ASK_SHARED_SECRET
 *   npx wrangler deploy
 */

const API_URL = "https://api.anthropic.com/v1/messages";
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

export default {
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
    if (request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      question = body.q || body.question || question;
      format = body.format || format;
    }
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
      return say("I can't reach my notes right now, so I'd only be guessing.", 502);
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
      // Spoken aloud, so it says what happened in a sentence rather than
      // reading a stack trace at you.
      return say("Something went wrong reaching the model. Try again in a moment.", 502);
    }

    const answer = (data.content || [])
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join("\n")
      .trim();

    return say(answer || "I don't have an answer for that.");
  },
};
