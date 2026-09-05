// Project Overseer dashboard — fetches the latest digest the weekly run
// committed, renders it readably, and lets you opt into push notifications.

const $ = (id) => document.getElementById(id);

// Last digest loaded, kept so the Copy button can assemble a plain-text version.
let latestDigest = null;
// Previous runs shown in the "Previous runs" log, indexed by their Copy button.
let priorRuns = [];

function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
const escapeAttr = (s) => escapeHtml(s).replace(/"/g, "&quot;");

// ── GitHub links in digest/timeline text ──────────────────────────────────
// Repos the run actually touched, harvested from the timeline's file_issue /
// propose_enhancement / search rows (their text starts with the repo slug —
// see tracer._tool_summary). Used to turn "ufc-dashboard (#25)" in the digest
// into a tappable link to the real issue.
let repoMap = { slugs: new Set(), byShort: new Map() };

function buildRepoMap(d) {
  const slugs = new Set();
  const byShort = new Map();
  for (const t of (d && d.timeline) || []) {
    const txt = String(t.text || "");
    const m = txt.match(/^([\w.-]+\/[\w.-]+) — /) || txt.match(/^searched ([\w.-]+\/[\w.-]+):/);
    if (!m) continue;
    slugs.add(m[1]);
    byShort.set(m[1].split("/")[1].toLowerCase(), m[1]);
  }
  return { slugs, byShort };
}

const escapeRegExp = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// Linkify one already-HTML-escaped line: known repo slugs become repo links,
// and once a line is associated with a repo, its "#123" references become
// issue links. Resolution is per line so a digest bullet like
// "ufc-dashboard (#25): …" links #25 to the right repo.
function linkifyLine(escaped) {
  let html = escaped;
  let lineRepo = null;
  for (const slug of repoMap.slugs) {
    if (!html.includes(slug)) continue;
    lineRepo = lineRepo || slug;
    html = html.split(slug).join(
      `<a href="https://github.com/${slug}" target="_blank" rel="noopener">${slug}</a>`);
  }
  if (!lineRepo) {
    for (const [short, slug] of repoMap.byShort) {
      const re = new RegExp(`(^|[^\\w/-])${escapeRegExp(short)}(?![\\w-])`, "i");
      if (re.test(html)) { lineRepo = slug; break; }
    }
  }
  if (lineRepo) {
    html = html.replace(/#(\d+)\b/g,
      `<a href="https://github.com/${lineRepo}/issues/$1" target="_blank" rel="noopener">#$1</a>`);
  }
  return html;
}

// Tiny inline-SVG sparkline for week-over-week trends (overseer #6). Pass lo/hi
// to pin the y-axis (e.g. 0..1 for health scores) so magnitude reads honestly;
// omit them to auto-scale (e.g. issue/enhancement counts). Returns "" with <2
// points — a single dot isn't a trend.
function sparkline(values, { width = 96, height = 22, stroke = "#60a5fa", lo = null, hi = null } = {}) {
  const vals = values.filter((v) => typeof v === "number");
  if (vals.length < 2) return "";
  const min = lo != null ? lo : Math.min(...vals);
  const max = hi != null ? hi : Math.max(...vals);
  const span = (max - min) || 1;
  const stepX = width / (vals.length - 1);
  const y = (v) => (height - 3 - ((v - min) / span) * (height - 6)).toFixed(1);
  const pts = vals.map((v, i) => `${(i * stepX).toFixed(1)},${y(v)}`).join(" ");
  const last = vals[vals.length - 1];
  return `<svg class="spark" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
    <polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${((vals.length - 1) * stepX).toFixed(1)}" cy="${y(last)}" r="2.2" fill="${stroke}"/>
  </svg>`;
}

// Health-score line colour matches the project's current state.
const SCORE_STROKE = { ok: "#34d399", idle: "#fbbf24", stale: "#fb7185", error: "#f87171", blind: "#fb923c" };

// The three pipeline agents, used to colour-code and group the timeline.
const AGENTS = {
  "Bug-Hunter": { slug: "bug-hunter", label: "🐛 Bug-Hunter" },
  "Idea-Agent": { slug: "idea-agent", label: "💡 Idea Agent" },
  "Reviewer":   { slug: "reviewer",   label: "📋 Reviewer" },
};
const agentSlug = (name) => (AGENTS[name] && AGENTS[name].slug) || "";
const agentLabel = (name) => (AGENTS[name] && AGENTS[name].label) || name;

// Turn the plain-text digest into headings + bullet lists so it's scannable.
function formatDigest(text) {
  const lines = String(text).split("\n");
  let html = "";
  let inList = false;
  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { closeList(); continue; }

    // Bullets ("- ", "• ") and ranked items ("1. ", "2) ") both become list rows.
    const bullet = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (bullet) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${linkifyLine(escapeHtml(bullet[1]))}</li>`;
      continue;
    }

    closeList();
    // Section heading: an all-caps line ("ISSUES FOUND") or one of the Reviewer's
    // title-case section headers ("Issues Found", "Top Enhancement Ideas (ranked)").
    const isHeading = /^[A-Z][A-Z0-9 /&,'()-]*$/.test(line) ||
      /^(issues found|top enhancement ideas)\b/i.test(line);
    if (isHeading) {
      html += `<h3>${escapeHtml(line)}</h3>`;
    } else {
      html += `<p>${linkifyLine(escapeHtml(line))}</p>`;
    }
  }
  closeList();
  return html || "<p>(no summary)</p>";
}

// Assemble the "pertinent details" as plain text for the Copy button — the
// digest summary plus project health, run counts and the run time, so it drops
// cleanly into a note or message from your phone.
function buildCopyText(d) {
  const lines = [(d.summary || "No run yet.").trim()];

  const projects = d.projects || {};
  const names = Object.keys(projects);
  if (names.length) {
    lines.push("", "Project health");
    for (const name of names) {
      const p = projects[name] || {};
      const badge = String(p.status || "unknown").toUpperCase();
      lines.push(`- ${name}: ${badge}${p.reason ? " — " + p.reason : ""}`);
    }
  }

  const c = d.counts || {};
  lines.push("",
    `Tool calls: ${c.tools ?? 0} · Issues filed: ${c.issues ?? 0} · ` +
    `Enhancements: ${c.enhancements ?? 0} · Errors: ${c.errors ?? 0}`);

  if (d.generated) {
    lines.push("",
      `Last run: ${new Date(d.generated).toLocaleString()}` +
      (d.status ? " — " + d.status : ""));
  }
  return lines.join("\n");
}

// Flatten the "what the agents did" timeline into readable plain text, grouped
// by agent, for the timeline card's Copy button.
function buildTimelineText(d) {
  const timeline = (d && d.timeline) || [];
  if (!timeline.length) return "What the agents did\n\n(no run yet)";
  const lines = ["What the agents did — Bug-Hunter → Idea → Reviewer"];
  let lastAgent = null;
  for (const t of timeline) {
    const agent = t.agent || "";
    if (agent !== lastAgent) {
      lastAgent = agent;
      lines.push("", agentLabel(agent) || agent || "—");
    }
    const label = String(t.label || "").trim();
    const text = String(t.text_full || t.text || "").trim();
    lines.push(`- ${t.ts}${label ? " · " + label : ""}${text ? ": " + text : ""}`);
  }
  if (d.generated) {
    lines.push("", `Last run: ${new Date(d.generated).toLocaleString()}` +
      (d.status ? " — " + d.status : ""));
  }
  return lines.join("\n");
}

function flashCopyBtn(btn, label, ok) {
  clearTimeout(btn._resetTimer);
  btn.textContent = label;
  btn.classList.toggle("copied", ok);
  btn._resetTimer = setTimeout(() => {
    btn.textContent = "Copy";
    btn.classList.remove("copied");
  }, 1600);
}

async function copyText(text, btn) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      // Fallback for older / non-secure-context mobile browsers.
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    flashCopyBtn(btn, "Copied!", true);
  } catch (e) {
    flashCopyBtn(btn, "Copy failed", false);
  }
}

function copyRecord(record, btn) {
  copyText(buildCopyText(record), btn);
}

// Native share sheet (Messages / Notes / mail) — one tap fewer than
// copy-switch-paste on phones. The Share buttons stay hidden on browsers
// without navigator.share, where Copy already covers it.
const canShare = !!navigator.share;

function shareText(text) {
  navigator.share({ title: "Project Overseer", text }).catch(() => { /* user cancelled */ });
}

function copyDigest() {
  const btn = $("copy-digest");
  if (!latestDigest) { flashCopyBtn(btn, "Nothing yet", false); return; }
  copyRecord(latestDigest, btn);
}

function copyTimeline() {
  const btn = $("copy-timeline");
  if (!latestDigest) { flashCopyBtn(btn, "Nothing yet", false); return; }
  copyText(buildTimelineText(latestDigest), btn);
}

// Render the "Previous runs" log from the history file. The last history record
// is the current run (already shown in "Latest digest"), so the archive lists
// everything before it, most recent first, each an expandable digest.
function renderHistory(runs) {
  priorRuns = runs.slice(0, -1).reverse();
  if (!priorRuns.length) return;
  $("history-card").style.display = "";
  $("history-log").innerHTML = priorRuns.map((r, i) => {
    const when = r.generated
      ? new Date(r.generated).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
      : (r.date || "");
    const c = r.counts || {};
    const meta =
      `${c.issues ?? 0} issue${c.issues === 1 ? "" : "s"} · ` +
      `${c.enhancements ?? 0} idea${c.enhancements === 1 ? "" : "s"}` +
      (r.status && r.status !== "completed" ? " · " + escapeHtml(r.status) : "");
    const body = r.summary
      ? formatDigest(r.summary)
      : '<p class="muted">Digest text wasn\'t recorded for this run.</p>';
    const btns = r.summary
      ? `<div class="head-btns" style="margin-top:12px">` +
        (canShare ? `<button class="copy-btn run-share" type="button" data-run="${i}">Share</button>` : "") +
        `<button class="copy-btn run-copy" type="button" data-run="${i}">Copy</button></div>`
      : "";
    return `<details class="run">
      <summary>
        <span><span class="rdate">${escapeHtml(when)}</span>
          <span class="rmeta">${meta}</span></span>
        <span class="rchevron">▶</span>
      </summary>
      <div class="rbody"><div class="digest">${body}</div>${btns}</div>
    </details>`;
  }).join("");
}

// "2 days ago" reads faster than a full timestamp when the real question is
// "is this fresh, or did a run get missed?"
function relativeTime(iso) {
  const t = new Date(iso).getTime();
  if (!isFinite(t)) return "";
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 90) return "just now";
  const m = Math.round(s / 60);
  if (m < 90) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 36) return `${h} hour${h === 1 ? "" : "s"} ago`;
  const days = Math.round(h / 24);
  if (days < 11) return `${days} day${days === 1 ? "" : "s"} ago`;
  const w = Math.round(days / 7);
  return `${w} week${w === 1 ? "" : "s"} ago`;
}

// The pipeline runs weekly, so anything older than 8 days means a missed run —
// tint the header line amber and say so instead of leaving a quiet stale date.
function renderGenerated(d) {
  const el = $("generated");
  const ageDays = (Date.now() - new Date(d.generated).getTime()) / 86400000;
  const overdue = isFinite(ageDays) && ageDays > 8;
  const exact = new Date(d.generated).toLocaleString(undefined,
    { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  el.textContent = `Last run: ${relativeTime(d.generated)} (${exact}) — ${d.status || ""}` +
    (overdue ? " · next run overdue" : "");
  el.classList.toggle("stale", overdue);
}

// Shipped ledger — what became of the proposals the pipeline filed.
//
// A month of digests otherwise reads as a pile of suggestions with no evidence
// any of them mattered. This is the other half of the record: proposed vs.
// actually delivered. "Shipped" deliberately means a MERGED fix, not just a
// closed issue — a fix sitting on an unreviewed branch is in flight, and
// claiming it as delivered would make this panel flattering instead of useful.
const SHIPPED_STATES = {
  shipped:     { label: "shipped",     cls: "ok" },
  in_flight:   { label: "in flight",   cls: "warn" },
  open:        { label: "open",        cls: "" },
  duplicate:   { label: "duplicate",   cls: "" },
  not_planned: { label: "not planned", cls: "" },
};

function renderShipped(ledger) {
  if (!ledger || !ledger.entries || !ledger.entries.length) return;
  const t = ledger.totals || {};
  $("shipped-card").style.display = "";

  const pct = (v) => `${Math.round((v || 0) * 100)}%`;

  // Lead with the one number that answers "is this thing delivering", drawn as
  // a bar. Six equal-sized tiles made the reader divide 44 by 70 themselves to
  // find out. The breakdown stays underneath for anyone who wants it.
  //
  // The bar's segments are exactly delivery_rate's denominator — everything
  // except duplicates (see tools.delivery_ledger). That matters: sizing the bar
  // on shipped+in_flight+open alone would fill it to 88% while the label beside
  // it read 82%, and a reader who checked would be right to distrust both.
  const parts = [
    ["s-shipped", "shipped", t.shipped || 0],
    ["s-flight", "in flight", t.in_flight || 0],
    ["s-open", "open", t.open || 0],
    ["s-noplan", "not planned", t.not_planned || 0],
  ];
  const considered = parts.reduce((a, [, , v]) => a + v, 0);
  const bar =
    `<div class="stat-hero"><b>${t.shipped ?? 0}</b> shipped of ${considered}` +
    ` · <span class="hero-pct">${pct(t.delivery_rate)} delivered</span></div>` +
    `<div class="dbar">` +
    parts.map(([c, , v]) => considered
      ? `<span class="${c}" style="width:${(v / considered) * 100}%"></span>` : "").join("") +
    `</div>` +
    `<div class="dkey">` +
    parts.filter(([, , v]) => v > 0)
      .map(([c, l, v]) => `<span><i class="${c}"></i>${v} ${l}</span>`).join("") +
    `</div>`;

  // Duplicates are the one thing outside the bar: a re-proposal of an existing
  // idea isn't work, it's a dedupe failure, and counting it would dilute the
  // rate rather than describe it.
  const aside = `<div class="sub">${t.proposed ?? 0} filed all time` +
    (t.duplicate ? ` · ${t.duplicate} duplicate (${pct(t.duplicate_rate)})` : "") +
    `</div>` + deliverySplit(t);

  const head = bar + aside;

  // Grouped by project, and EVERY item rendered — a capped list answers "how are
  // we doing" but not "what happened to this specific idea", which is the
  // question the panel exists for. Each repo is a <details> so the page stays
  // scannable while still holding the whole record.
  const order = { shipped: 0, in_flight: 1, open: 2, duplicate: 3, not_planned: 4 };
  const byRepo = new Map();
  for (const e of ledger.entries) {
    const short = (e.repo || "?").split("/").pop();
    if (!byRepo.has(short)) byRepo.set(short, []);
    byRepo.get(short).push(e);
  }

  const row = (e) => {
    const st = SHIPPED_STATES[e.status] || { label: e.status, cls: "" };
    const when = e.closed_at || e.created_at;
    const ago = when ? relativeTime(when) : "";
    // WHERE it landed. A "shipped" badge with no link still leaves you digging
    // through commit history for the change it is referring to.
    const fix = e.fix_url
      ? ` · <a href="${escapeHtml(e.fix_url)}" target="_blank" rel="noopener" class="fix">${escapeHtml(e.fix_ref || "fix")}</a>`
      : "";
    // WHY, for anything that wasn't simply built — carried on the row so a
    // decision isn't buried in an issue comment nobody opens.
    const why = e.decision
      ? `<div class="pmeta why">${escapeHtml(e.decision)}</div>` : "";
    return `<div class="prow${e.status === "shipped" ? "" : " dim"}">
      <div>
        <div class="pname"><span class="rc ${st.cls}">${st.label}</span>
          <a href="${escapeHtml(e.url)}" target="_blank" rel="noopener">${escapeHtml(e.title)}</a></div>
        <div class="pmeta">#${e.number}${ago ? " · " + escapeHtml(ago) : ""}${fix}</div>
        ${why}
      </div>
    </div>`;
  };

  // Per-repo proposal yield (#60), computed by tools.proposal_outcomes and
  // published with the ledger. Keyed by full slug, matched to the short name
  // this panel groups by.
  const outcomes = new Map(
    Object.entries(((ledger.outcomes || {}).by_repo) || {})
      .map(([slug, o]) => [slug.split("/").pop(), o]));

  const groups = [...byRepo.entries()].sort().map(([repo, items]) => {
    items.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || b.number - a.number);
    const shipped = items.filter((e) => e.status === "shipped").length;
    const pending = items.filter((e) => e.status === "open" || e.status === "in_flight").length;
    // "8/19 shipped" counts everything filed including untriaged ideas; the
    // yield counts only proposals that got an answer. Both are shown because
    // they answer different questions, and the second is the one fed back into
    // the idea agent's prompt. A rate is withheld below the sample floor rather
    // than printed at three items, where it is arithmetic and not evidence.
    const o = outcomes.get(repo);
    const yield_ = o && o.ship_rate !== null && o.ship_rate !== undefined
      ? ` · ideas ship at ${Math.round(o.ship_rate * 100)}%`
      : "";
    return `<details class="repo-group">
      <summary><strong>${escapeHtml(repo)}</strong>
        <span class="pmeta">${shipped}/${items.length} shipped${pending ? ` · ${pending} pending` : ""}${yield_}</span>
      </summary>
      ${items.map(row).join("")}
    </details>`;
  }).join("");

  $("shipped").innerHTML = head + groups;
}

// When the dispatcher last ran — the fact that makes the rest of the panel
// readable.
//
// An empty "Under way" list has two opposite meanings: the implementer ran and
// there was nothing to hand over, or the implementer never ran at all. Those
// rendered identically until this line existed, and on 2026-08-31 the second one
// was true for hours while the panel looked like a calm week. The dispatch cron
// is skipped often enough (GitHub deprioritises schedules on free public repos)
// that "when did this last fire" is load-bearing, not trivia.
//
// The age is computed HERE, at render time, from the timestamp the ledger
// publishes. An age baked in at build time would still say "2 hours ago" on
// Thursday.
function dispatchLine(d) {
  if (d === undefined) return "";              // ledger predates this field
  if (d === null) {
    return `<div class="sub dim">Last dispatch: unknown — could not read the ` +
           `workflow's run history.</div>`;
  }
  const at = new Date(d.at);
  if (isNaN(at)) return "";
  const days = Math.floor((Date.now() - at) / 86400000);
  const when = days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
  // A week is the cadence, so anything past it means a Monday was missed.
  const cls = days > 7 ? "bad" : days > 1 ? "warn" : "";
  const failed = d.conclusion && d.conclusion !== "success"
    ? ` · ended <b>${escapeHtml(d.conclusion)}</b>` : "";
  return `<div class="sub"><span class="rc ${cls}">last dispatch</span> ` +
         `${escapeHtml(when)}${failed}</div>`;
}

// How much of "shipped" the implementer actually delivered (#66).
//
// The bar in the delivery panel counts merged work and says nothing about where
// it came from, which credited the pipeline for an evening of hand-written
// fixes. The implementer is ~4.4x the cost of the whole review, so this is the
// number that decides whether it earns its bill.
//
// Both figures come from tools.delivery_ledger; nothing here re-reads a label
// (invariant 4). A ledger published before the split existed carries neither
// count, and renders nothing rather than a confident zero.
function deliverySplit(t) {
  const auto = t.shipped_by_implementer;
  const hand = t.shipped_by_hand;
  if (typeof auto !== "number" || typeof hand !== "number") return "";
  if (!auto && !hand) return "";
  // The pre-dispatcher count is carried in the same sentence on purpose. Without
  // it "1 by the implementer" sits next to a shipped total of 49 and reads as a
  // failure rate, when most of that total landed before the implementer existed.
  const before = t.shipped_before_implementer;
  const tail = before ? ` \u00b7 ${before} shipped before it existed` : "";
  return `<div class="sub dim">since the implementer: ` +
    `${auto} by the implementer \u00b7 ${hand} by hand${tail}</div>`;
}

// Implementer — the queue between "proposed" and "shipped".
//
// The Shipped panel is a record of what happened; this is the only view of what
// is ABOUT to happen, which is the half you can still change your mind about.
// Every figure comes from the `queue` block the ledger publishes
// (tools.queue_state), never from rules re-implemented here — a second copy of
// the gate in this file would drift from the dispatcher's and describe a queue
// that never runs.
function renderImplementer(q) {
  if (!q) return;
  $("implementer-card").style.display = "";

  const n = (a) => (a || []).length;
  const cost = (q.cost_hint || 0) * n(q.next);

  // Lead with the two numbers worth acting on: what the next run will attempt,
  // and what it will cost. The gate that produced them goes underneath.
  const head =
    `<div class="stat-hero"><b>${n(q.next)}</b> queued for the next run` +
    (cost ? ` · <span class="hero-pct">~$${cost.toFixed(2)}</span>` : "") + `</div>` +
    `<div class="sub">bugs + effort:${escapeHtml((q.efforts || []).join("/"))}` +
    ` · cap ${q.cap} per run · ${q.eligible} eligible · ` +
    `${escapeHtml(q.tier || "light")} tier</div>`;

  const row = (e, badge, cls) => {
    const sizing = [e.effort && `effort:${e.effort}`, e.impact && `impact:${e.impact}`]
      .filter(Boolean).join(" · ");
    const fix = e.fix_url
      ? ` · <a href="${escapeHtml(e.fix_url)}" target="_blank" rel="noopener" class="fix">${escapeHtml(e.fix_ref || "fix")}</a>`
      : "";
    return `<div class="prow">
      <div>
        <div class="pname"><span class="rc ${cls}">${badge}</span>
          <a href="${escapeHtml(e.url || "#")}" target="_blank" rel="noopener">${escapeHtml(e.title || "")}</a></div>
        <div class="pmeta">${escapeHtml((e.repo || "").split("/").pop())} #${e.number}` +
          `${sizing ? " · " + escapeHtml(sizing) : ""}${fix}</div>
      </div>
    </div>`;
  };

  const section = (title, items, badge, cls, empty) =>
    n(items)
      ? `<div class="qgroup"><div class="pmeta qhead">${title}</div>` +
        items.map((e) => row(e, badge, cls)).join("") + `</div>`
      : (empty ? `<div class="qgroup"><div class="pmeta qhead">${title}</div>` +
                 `<div class="pmeta dim">${empty}</div></div>` : "");

  // "Nothing queued" has two very different meanings and the panel must not
  // blur them: an empty backlog is success, while a backlog full of work the
  // gate won't touch is a setting you may want to change.
  const nothing = q.eligible
    ? "Nothing left to pick this run — the eligible items are already under way."
    : "Nothing eligible: everything filed is either bigger than the effort gate, " +
      "already under way, or done.";

  $("implementer").innerHTML = head + dispatchLine(q.last_dispatch) +
    section("Next run", q.next, "queued", "", nothing) +
    section("Under way", q.in_flight, "building", "warn") +
    section("Needs you", q.benched, "stalled", "bad",  "") +
    (n(q.benched)
      ? `<div class="pmeta dim">Remove the <code>${escapeHtml(q.failed_label || "")}</code>` +
        ` label to put these back in the queue.</div>` : "");
}

// Model spend — what the run cost, and what the two-tier split saved.
//
// The pipeline runs three agents; only the Bug-Hunter's calls are consequential
// enough to need the heavy model, so the other two run on a cheaper one. That
// is a claim, and this panel is the evidence for it: the baseline reprices this
// run's ACTUAL token counts at the heavy rate, so the saving is arithmetic
// rather than an assertion. It stays hidden on older digests that predate the
// accounting, and on any run whose model isn't in the rate card — a confident
// $0.00 would be worse than showing nothing.
function renderSpend(spend, runs, queue) {
  if (!spend || !spend.agents || !spend.agents.length) return;
  $("spend-card").style.display = "";

  const usd = (v) => (v == null ? "—" : `$${v < 0.01 ? v.toFixed(4) : v.toFixed(2)}`);

  // Same shape as the Shipped panel: one sentence carrying the answer, then the
  // supporting numbers. Reading two panels the same way is most of what makes a
  // dashboard feel legible.
  const hero = `<div class="stat-hero"><b>${usd(spend.total_usd)}</b> this run` +
    (spend.saved_pct != null
      ? ` · <span class="hero-pct">${spend.saved_pct}% cheaper</span> than running every` +
        ` agent heavy, saving ${usd(spend.saved_usd)}`
      : "") + `</div>`;

  const stats = [];
  // Cumulative saving is the number that actually justifies the change — one
  // run's few cents never will. Hidden until it covers more than the current
  // run, since "saved this run $0.11" beside "saved to date $0.11" reads as a
  // duplicated tile rather than a total.
  const scored = (runs || []).filter((r) => r.spend && r.spend.saved_usd != null);
  if (scored.length > 1) {
    stats.push([`saved over ${scored.length} runs`,
                usd(scored.reduce((a, r) => a + r.spend.saved_usd, 0))]);
  }

  const head = hero + (stats.length
    ? `<div class="stats">` + stats.map(([l, v]) =>
        `<div class="stat"><div class="n">${escapeHtml(v)}</div><div class="l">${escapeHtml(l)}</div></div>`
      ).join("") + `</div>`
    : "");

  const rows = spend.agents.map((a) => {
    const inTok = (a.input || 0) + (a.cache_write || 0) + (a.cache_read || 0);
    // Light is the cheap path, so it gets the green chip; heavy is a deliberate
    // choice rather than a problem, so it stays neutral — not amber.
    const cls = a.tier === "light" ? "ok" : "";
    return `<div class="prow">
      <div>
        <div class="pname"><span class="rc ${cls}">${escapeHtml(a.tier || "?")}</span>
          ${escapeHtml(a.agent)}</div>
        <div class="pmeta">${escapeHtml(a.model || "unknown model")} ·
          ${inTok.toLocaleString()} in / ${(a.output || 0).toLocaleString()} out ·
          ${a.calls || 0} call${a.calls === 1 ? "" : "s"}</div>
      </div>
      <div class="pname">${escapeHtml(a.usd == null ? "unpriced" : usd(a.usd))}</div>
    </div>`;
  }).join("");

  const note = `<div class="spend-note">Estimated from published list prices and this
    run's token counts — Anthropic's invoice is the authority. The baseline reprices the
    same tokens at ${escapeHtml(spend.heavy_model || "the heavy model")}.</div>`;

  // The implementer is the expensive half and it does NOT run in this process:
  // it runs in each project's own repo, so none of its spend reaches this
  // panel's token counts. Left unsaid, "$0.34 this run" reads as the week's
  // bill when the week is nearer $4.80 — a panel flattering itself, which is
  // the one thing the ledger's rules forbid. The figure is labelled an estimate
  // because it is one: everything else here is measured, and mixing the two
  // silently would be worse than leaving it out.
  const queued = (queue && queue.next && queue.next.length) || 0;
  let outside = "";
  if (queued) {
    outside = `<div class="spend-note">Excludes the implementer, which runs in each
      project's own repo: about <b>${usd((queue.cost_hint || 0) * queued)}</b> more for the
      ${queued} attempt${queued === 1 ? "" : "s"} queued in the Implementer panel —
      estimated from measured runs, not from billed tokens.</div>`;
  } else if (queue) {
    outside = `<div class="spend-note">Excludes the implementer, which runs in each
      project's own repo. Nothing is queued, so this is the week's model spend.</div>`;
  }

  $("spend").innerHTML = head + rows + note + outside;
}

// ── THE PLAIN HALF ───────────────────────────────────────────────────────
// Three questions, answered in words that assume nothing: is anything wrong,
// what got finished, what happens next.
//
// The VERDICT is published, not composed here (digest.headline, from
// attention.headline) — the sentence a stranger reads first is the last one
// that should exist in two languages. What this function does is the rendering
// the published facts cannot do for themselves: pick the label, count what is
// already counted, and say when each half was last checked.

// "17 days ago" beats a timestamp, and both beat a number of hours. Whole days
// only: nobody acts differently on 17.4 days than on 17.
function plainAge(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  if (!isFinite(ms)) return null;
  const mins = Math.round(ms / 60000);
  if (mins < 90) return mins <= 1 ? "just now" : `${mins} minutes ago`;
  const hours = Math.round(mins / 60);
  if (hours < 36) return hours === 1 ? "an hour ago" : `${hours} hours ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

function renderPlain(d, ledger) {
  // The published verdict. Falls back to a health count for a digest written
  // before headline existed, rather than showing an empty banner.
  const rollup = d.rollup || {};
  const ranked = d.attention || [];
  const headline = d.headline
    || (rollup.total ? `${rollup.ok} of ${rollup.total} projects look fine.` : "");
  const worried = ranked.some((a) => a.notable)
    || (rollup.attention || []).some((a) => a.nudge);
  const verdict = $("plain-verdict");
  verdict.textContent = headline || "No run yet.";
  verdict.className = "plain-verdict " + (headline ? (worried ? "warn" : "good") : "");

  const out = [];

  // What to DO about it, for the project the headline names. A page that says
  // something is wrong and stops there makes the reader work out the next step
  // every single time.
  const top = ranked[0];
  if (top && top.action && worried) {
    out.push(`<div class="plain-action">${escapeHtml(top.action)}</div>`);
  }

  // EVERY project, in the order the score ranked them. The headline names one
  // and counts the rest, which answers "is anything wrong" but not "and how is
  // everything else" — and four projects is a short enough list to just show.
  // `short` and `notable` are both published (attention.plain_predicate): the
  // page must not decide for itself which projects are a concern, or it will
  // eventually disagree with the sentence directly above it.
  if (ranked.length) {
    out.push(section("Your projects", ranked.map((a) => `
      <div class="prow-plain${a.notable ? " flag" : ""}">
        <span class="pn">${escapeHtml(a.name)}</span>
        <span class="pd">${escapeHtml(a.short || "")}</span>
      </div>`).join("")));
  }

  // WHAT IT FINISHED, with names. "61 jobs so far" is a scoreboard; the titles
  // are what tells you whether the work was any good.
  const entries = (ledger && ledger.entries) || [];
  const shipped = entries
    .filter((e) => e.status === "shipped" && e.closed_at)
    .sort((a, b) => (a.closed_at < b.closed_at ? 1 : -1));
  const t = (ledger && ledger.totals) || {};
  if (shipped.length) {
    const recent = shipped.slice(0, 4).map((e) => itemRow(e.title, e.repo, relativeTime(e.closed_at)));
    const flight = t.in_flight
      ? `<div class="plain-note">${t.in_flight} more finished and waiting to be checked.</div>`
      : "";
    out.push(section(`Recently finished — ${t.shipped || shipped.length} in total`,
                     recent.join("") + flight));
  }

  // WHAT IS COMING, with titles. These are written by an agent for an engineer
  // and run long, so they get their own rows rather than being squeezed into a
  // sentence — one of them filled six lines of a phone screen when it was.
  const q = (ledger && ledger.queue) || null;
  if (q) {
    const next = q.next || [];
    const body = next.length
      ? next.map((e) => itemRow(e.title, e.repo, null)).join("")
      : `<div class="plain-note">Nothing is lined up to be built right now.</div>`;
    out.push(section("It will build next", body));
  }

  $("plain-lines").innerHTML = out.join("");

  // TWO clocks, said out loud. The health above refreshes several times a day;
  // the written review is weekly. Showing one date for both is what made a
  // five-day-old panel look current — the whole reason the refresh exists.
  const checked = d.refreshed ? plainAge(d.refreshed) : null;
  const reviewed = d.generated ? plainAge(d.generated) : null;
  $("plain-checked").textContent = [
    checked ? `Checked ${checked}` : null,
    reviewed ? `full review ${reviewed}` : null,
  ].filter(Boolean).join(" · ");
}

function section(title, body) {
  return `<div class="plain-section">
    <div class="plain-head">${escapeHtml(title)}</div>${body}</div>`;
}

// One piece of work: what it is, which project, and when. The title is an issue
// title — long, and written for an engineer — so it wraps onto its own line
// rather than being truncated into something even less readable.
function itemRow(title, repo, when) {
  const where = (repo || "").split("/").pop();
  const meta = [where, when].filter(Boolean).join(" · ");
  return `<div class="item-plain">
    <div class="it">${escapeHtml(title || "")}</div>
    ${meta ? `<div class="im">${escapeHtml(meta)}</div>` : ""}
  </div>`;
}

async function loadDigest() {
  try {
    const res = await fetch("digest.json?" + Date.now()); // bust cache
    if (!res.ok) throw new Error(res.status);
    const d = await res.json();
    latestDigest = d;
    repoMap = buildRepoMap(d);

    // Week-over-week history for the trend sparklines (overseer #6). Optional —
    // it doesn't exist until the first run after history tracking shipped.
    let history = null;
    try {
      const hres = await fetch("history.json?" + Date.now());
      if (hres.ok) history = await hres.json();
    } catch (e) { /* no history yet */ }

    // Delivery ledger (optional — absent until the first run after it shipped).
    let ledger = null;
    try {
      const lres = await fetch("shipped.json?" + Date.now());
      if (lres.ok) ledger = await lres.json();
    } catch (e) { /* no ledger yet */ }
    const runs = (history && history.runs) || [];
    const scoreSeries = (name) =>
      runs.map((r) => (r.projects && r.projects[name] ? r.projects[name].score : null));

    renderGenerated(d);
    renderPlain(d, ledger);
    renderShipped(ledger);
    renderImplementer(ledger && ledger.queue);
    renderSpend(d.spend, runs, ledger && ledger.queue);
    $("digest").innerHTML = formatDigest(d.summary || "");

    const c = d.counts || {};
    $("stats").innerHTML = [
      ["tools", "tool calls"],
      ["issues", "issues filed"],
      // Raw count of propose_enhancement calls this run — the Reviewer curates
      // a subset into the digest, so label it "ideas proposed" (not just
      // "enhancements") so the tile never reads as contradicting the digest.
      ["enhancements", "ideas proposed"],
      ["errors", "errors"],
    ].map(([k, label]) =>
      `<div class="stat"><div class="n">${c[k] ?? 0}</div><div class="l">${label}</div></div>`
    ).join("");

    // Top-of-dashboard rollup — the run "at a glance" so a regression (e.g. a
    // project gone idle for several cycles) is visible immediately instead of
    // buried in the timeline. Reuses the server-computed health flags.
    const rollup = d.rollup;
    const nudgeAt = (rollup && rollup.nudge_threshold) || 2;
    if (rollup) {
      const att = rollup.attention || [];
      // Health only. The issues/ideas counts used to be repeated here as chips
      // AND as tiles directly below, so the same two numbers appeared twice
      // within one screen. The glance answers "is anything wrong"; the tiles
      // below answer "what did the run do".
      // A blocked duplicate is the one visible sign the dedupe gate did anything
      // (#33) — without it the check looks identical to a week with no
      // re-proposals, which is how a working guard gets removed as pointless.
      const blocked = (d.counts || {}).duplicates_blocked || 0;
      const chips = [
        `<span class="rc ok">${rollup.ok}/${rollup.total} healthy</span>`,
        att.length ? `<span class="rc warn">${att.length} need${att.length === 1 ? "s" : ""} attention</span>` : "",
        blocked ? `<span class="rc">${blocked} duplicate idea${blocked === 1 ? "" : "s"} blocked</span>` : "",
      ].filter(Boolean).join("");
      // Only projects past the nudge threshold get an explicit call-out row.
      const nudges = att.filter((a) => a.nudge).map((a) => {
        const st = ["idle", "stale", "blind", "error"].includes(a.status) ? a.status : "blind";
        const badge = { idle: "IDLE", stale: "STALE", blind: "BLIND", error: "ERROR" }[st];
        return `<div class="nudge ${st}"><span class="pbadge ${st}">${badge}</span>
          <span class="ntext"><b>${escapeHtml(a.name)}</b> — ${escapeHtml(a.detail)}</span></div>`;
      }).join("");
      // An agent that reasoned, wrote a confident summary, and filed nothing.
      // This sits with the project nudges because it is the same failure shape:
      // the run looks healthy and quietly delivered less than it claims.
      const silent = (d.output_alerts || []).map((a) =>
        `<div class="nudge blind"><span class="pbadge blind">SILENT</span>
          <span class="ntext"><b>${escapeHtml(a.agent)}</b> — ${escapeHtml(a.detail)}</span></div>`
      ).join("");
      $("rollup").innerHTML = `<div class="rollup-chips">${chips}</div>` +
        (nudges || silent ? `<div class="nudges">${nudges}${silent}</div>` : "");
      $("rollup-card").style.display = "";
    }

    // Per-project health with BLIND badges (blind-spot tracking), ordered by the
    // attention score the run published (#25).
    //
    // The ORDER and the "why" both come from Python (tracer.attention →
    // attention.rank). This panel must never compute a score of its own: it is
    // the same rule the implementation gate lives under (invariant 4), and a
    // second scoring formula in JavaScript would drift from the one the digest
    // and the voice assistant quote, leaving three answers to "what should I
    // work on" that disagree.
    const projects = d.projects || {};
    const attention = new Map((d.attention || []).map((a) => [a.name, a]));
    const names = Object.keys(projects).sort((a, b) => {
      const sa = attention.get(a), sb = attention.get(b);
      if (!sa || !sb) return 0;                       // digest predates the field
      return (sb.score || 0) - (sa.score || 0) || a.localeCompare(b);
    });
    if (names.length) {
      $("projects-card").style.display = "";
      $("projects").innerHTML = names.map((name) => {
        const p = projects[name];
        const st = ["ok", "idle", "stale", "error", "blind"].includes(p.status) ? p.status : "blind";
        const badge = { ok: "OK", idle: "IDLE", stale: "STALE", error: "ERROR", blind: "BLIND" }[st];
        const lastOk = p.last_ok ? " · last ok " + new Date(p.last_ok).toLocaleDateString() : " · never read";
        let meta, alert = "";
        if (st === "ok") {
          meta = "healthy";
        } else if (st === "stale") {
          // Past-due data is actionable now, so it always reads as an alert.
          // Show how far past its freshness SLA the feed is when we know it.
          const fmtH = (h) => (Number.isFinite(h) ? `${+(+h).toFixed(1)}h` : null);
          const age = fmtH(p.age_hours), sla = fmtH(p.sla_hours);
          const past = age ? `data ${age} old${sla ? ` · SLA ${sla}` : ""}` : "data past-due";
          meta = `${past} · stale ${p.stale_cycles || 0} cycle${(p.stale_cycles === 1) ? "" : "s"}`;
          alert = " alert";
        } else if (st === "idle") {
          meta = "no recent activity" + ((p.idle_cycles || 0) >= nudgeAt ? ` · idle ${p.idle_cycles} cycles` : "");
          if ((p.idle_cycles || 0) >= nudgeAt) alert = " alert";
        } else if (st === "error") {
          meta = p.reason || "read failed";
          alert = " alert";
        } else {
          meta = (p.reason || "no data") + ((p.blind_cycles || 0) >= nudgeAt ? ` · blind ${p.blind_cycles} cycles` : "") + lastOk;
          if ((p.blind_cycles || 0) >= nudgeAt) alert = " alert";
        }
        const spark = sparkline(scoreSeries(name), { lo: 0, hi: 1, stroke: SCORE_STROKE[st] || "#94a3b8" });
        // The attention line answers "why is this one at the top", which is the
        // only thing that makes an ordering worth having. Rendered verbatim —
        // `why` is the scorer's own sentence about its own dominant signal.
        const a = attention.get(name);
        const why = a && a.score > 0
          ? `<div class="pmeta attn"><span class="ascore">${a.score.toFixed(2)}</span>
              ${escapeHtml(a.why || "")}</div>`
          : "";
        return `<div class="prow${alert}">
          <div><div class="pname">${escapeHtml(name)}</div>
            <div class="pmeta">${escapeHtml(meta)}</div>${why}</div>
          <div class="pright">${spark}<span class="pbadge ${st}">${badge}</span></div></div>`;
      }).join("");
    }

    // Trend card: issues + enhancements over the recorded run history (overseer #6).
    if (runs.length >= 2) {
      $("trends-card").style.display = "";
      const dates = runs.map((r) => r.date);
      const issues = runs.map((r) => (r.counts && r.counts.issues) || 0);
      const enh = runs.map((r) => (r.counts && r.counts.enhancements) || 0);
      const trow = (label, series, stroke) =>
        `<div class="trow"><span class="tlabel">${label}</span>
          ${sparkline(series, { stroke }) || '<span class="tnone">—</span>'}
          <span class="tlast">${series[series.length - 1]}</span></div>`;
      $("trends").innerHTML =
        trow("Issues filed", issues, "#f87171") +
        trow("Enhancements", enh, "#fbbf24") +
        `<div class="trange">${escapeHtml(dates[0])} → ${escapeHtml(dates[dates.length - 1])} · ${runs.length} runs</div>`;
    }

    // Previous-runs log — expandable archive of earlier digests (history log).
    renderHistory(runs);

    // Timeline grouped by agent — the pipeline runs Bug-Hunter → Idea → Reviewer
    // in order, so a header is emitted each time the agent changes.
    let lastAgent = null;
    $("timeline").innerHTML = (d.timeline || []).map((t) => {
      const m = String(t.label || "").match(/^(.*?)\s*\(([^)]+)\)\s*$/);
      const name = m ? m[1] : (t.label || "");
      const cat = m ? m[2] : "";
      const known = ["idea", "bug", "investigate", "error", "search", "digest"].includes(cat) ? cat : "";
      const agent = t.agent || "";
      const slug = agentSlug(agent);
      let header = "";
      if (agent && agent !== lastAgent) {
        lastAgent = agent;
        header = `<div class="agent-head ${slug}">${escapeHtml(agentLabel(agent))}</div>`;
      }
      // Truncated reasoning ships its full text in text_full — render it
      // tap-to-expand instead of dead-ending at "…".
      const full = t.text_full && t.text_full !== t.text ? t.text_full : null;
      const bodyAttrs = full
        ? ` class="body expandable" data-full="${escapeAttr(full)}" data-short="${escapeAttr(t.text)}"`
        : ' class="body"';
      return `${header}<div class="item ${slug}">
        <div class="meta">${cat ? `<span class="chip ${known}">${escapeHtml(cat)}</span>` : ""}
          <span>${escapeHtml(t.ts)} · ${escapeHtml(name)}</span></div>
        <div${bodyAttrs}>${linkifyLine(escapeHtml(t.text))}</div></div>`;
    }).join("");
    updateTimelineToggle();
  } catch (e) {
    // First-run empty state: explain what will appear and when, instead of a
    // dead end.
    $("generated").textContent = "Waiting for the first weekly run.";
    $("plain-verdict").textContent = "Nothing has run yet.";
    $("plain-lines").innerHTML =
      '<div class="plain-note">The first review runs on Monday, and this page ' +
      'fills in by itself.</div>';
    $("digest").innerHTML =
      "<p>No digest yet. This page fills in automatically after the weekly " +
      "review runs (Mondays 14:00 UTC via GitHub Actions) — you'll get the " +
      "digest, per-project health, and trends here.</p>" +
      "<p class=\"muted\">Already expecting data? Check the repo's Actions tab " +
      "to see if the run failed, or pull down / tap ↻ to reload.</p>";
  }
}

// The step-by-step trace is collapsed by default; the summary row says how
// much is behind it so collapsing doesn't hide that the data exists.
function updateTimelineToggle() {
  const n = ((latestDigest && latestDigest.timeline) || []).length;
  $("timeline-toggle").textContent = $("timeline-details").open
    ? "Hide steps"
    : (n ? `Show all ${n} steps` : "Show all steps");
}

// ── service worker (required for push + installability) ──────────────────
async function registerSW() {
  if ("serviceWorker" in navigator) {
    return navigator.serviceWorker.register("sw.js");
  }
}

// ── push subscription opt-in ──────────────────────────────────────────────
function urlBase64ToUint8Array(base64) {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function enablePush() {
  const help = $("notif-help");
  help.classList.remove("hidden");
  try {
    if (!("PushManager" in window)) {
      help.innerHTML = "This browser doesn't support push. On iPhone, first add this app to your Home Screen (Share → Add to Home Screen), then open it from there.";
      return;
    }
    const reg = await registerSW();
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { help.textContent = "Notifications were not allowed."; return; }

    const key = (await (await fetch("vapid-public.txt?" + Date.now())).text()).trim();
    if (!key) { help.textContent = "Server push key not set up yet (vapid-public.txt is empty)."; return; }

    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });

    $("sub").classList.remove("hidden");
    $("sub").value = JSON.stringify(sub);
    help.innerHTML =
      "<b>One-time step:</b> copy the text below and save it as a repository secret named " +
      "<code>PUSH_SUBSCRIPTION</code> (GitHub → Settings → Secrets and variables → Actions). " +
      "After that, the weekly run will push the digest here.";
  } catch (e) {
    help.textContent = "Couldn't subscribe: " + e.message;
  }
}

$("enable").addEventListener("click", enablePush);
$("copy-digest").addEventListener("click", copyDigest);
$("copy-timeline").addEventListener("click", copyTimeline);
if (canShare) {
  $("share-digest").classList.remove("hidden");
  $("share-timeline").classList.remove("hidden");
  $("share-digest").addEventListener("click", () => {
    if (latestDigest) shareText(buildCopyText(latestDigest));
  });
  $("share-timeline").addEventListener("click", () => {
    if (latestDigest) shareText(buildTimelineText(latestDigest));
  });
}
// Per-run Copy/Share buttons in the history log are rendered dynamically, so delegate.
$("history-log").addEventListener("click", (e) => {
  const btn = e.target.closest(".run-copy, .run-share");
  if (!btn) return;
  const run = priorRuns[Number(btn.dataset.run)];
  if (!run) return;
  if (btn.classList.contains("run-share")) shareText(buildCopyText(run));
  else copyRecord(run, btn);
});
$("timeline-details").addEventListener("toggle", updateTimelineToggle);

// Tap a truncated reasoning row to swap in its full text (and back).
$("timeline").addEventListener("click", (e) => {
  const body = e.target.closest(".body.expandable");
  if (!body || e.target.closest("a")) return;
  const open = body.classList.toggle("open");
  body.innerHTML = linkifyLine(escapeHtml(open ? body.dataset.full : body.dataset.short));
});

// Manual refresh — an installed PWA has no reload button, so give it one.
$("refresh").addEventListener("click", async () => {
  const btn = $("refresh");
  btn.disabled = true;
  btn.classList.add("spin");
  lastLoad = Date.now();
  try { await loadDigest(); } finally {
    setTimeout(() => { btn.classList.remove("spin"); btn.disabled = false; }, 400);
  }
});

// Push setup is a one-time action — once done (or dismissed), stop giving it
// space. Hiding swaps in a quiet footer link so it's always recoverable.
// localStorage can throw in some private-browsing modes; ignore that.
const PUSH_HIDDEN_KEY = "overseer-push-card-hidden";
function setPushCardHidden(hidden) {
  try {
    if (hidden) localStorage.setItem(PUSH_HIDDEN_KEY, "1");
    else localStorage.removeItem(PUSH_HIDDEN_KEY);
  } catch (e) { /* private mode */ }
  $("push-card").style.display = hidden ? "none" : "";
  $("push-restore").classList.toggle("hidden", !hidden);
}
try {
  if (localStorage.getItem(PUSH_HIDDEN_KEY)) setPushCardHidden(true);
} catch (e) { /* private mode */ }
$("push-hide").addEventListener("click", () => setPushCardHidden(true));
$("push-restore").addEventListener("click", () => setPushCardHidden(false));

// The details half is one toggle, and which way it was left is remembered per
// device — someone who wants the numbers wants them every time, and someone who
// does not should never see them again.
const DETAILS_KEY = "overseer-details-open";
function updateDetailsToggle() {
  const open = $("details").open;
  $("details-toggle").textContent = open ? "Hide the details" : "Show the details";
  try { open ? localStorage.setItem(DETAILS_KEY, "1") : localStorage.removeItem(DETAILS_KEY); }
  catch (e) { /* private mode */ }
}
try {
  if (localStorage.getItem(DETAILS_KEY)) $("details").open = true;
} catch (e) { /* private mode */ }
$("details").addEventListener("toggle", updateDetailsToggle);
updateDetailsToggle();

// Keep an open page current by itself.
//
// The data behind this page now moves several times a day (the status and
// ledger refreshes), but an installed PWA is a window that gets left open for
// days — it fetched once on launch and then showed whatever it had, with a
// manual ↻ as the only way forward. A phone brought out of a pocket showing
// Tuesday's health, with nothing on screen admitting it, is the same failure the
// refresh job exists to fix, one layer up.
//
// Refetching costs three small JSON files, so the rule is simply "not more than
// once a minute": when the page becomes visible again, and every five minutes
// while it is.
const MIN_RELOAD_GAP_MS = 60_000;
let lastLoad = Date.now();

async function reloadIfStale() {
  if (document.hidden) return;                 // never poll a backgrounded tab
  if (Date.now() - lastLoad < MIN_RELOAD_GAP_MS) return;
  lastLoad = Date.now();
  await loadDigest();
}

document.addEventListener("visibilitychange", reloadIfStale);
setInterval(reloadIfStale, 5 * 60_000);

registerSW();
loadDigest();
