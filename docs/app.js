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
const SCORE_STROKE = { ok: "#34d399", idle: "#fbbf24", error: "#f87171", blind: "#fb923c" };

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
      html += `<li>${escapeHtml(bullet[1])}</li>`;
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
      html += `<p>${escapeHtml(line)}</p>`;
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
    const text = String(t.text || "").trim();
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
    const copyBtn = r.summary
      ? `<button class="copy-btn run-copy" type="button" data-run="${i}" style="margin-top:12px">Copy</button>`
      : "";
    return `<details class="run">
      <summary>
        <span><span class="rdate">${escapeHtml(when)}</span>
          <span class="rmeta">${meta}</span></span>
        <span class="rchevron">▶</span>
      </summary>
      <div class="rbody"><div class="digest">${body}</div>${copyBtn}</div>
    </details>`;
  }).join("");
}

async function loadDigest() {
  try {
    const res = await fetch("digest.json?" + Date.now()); // bust cache
    if (!res.ok) throw new Error(res.status);
    const d = await res.json();
    latestDigest = d;

    // Week-over-week history for the trend sparklines (overseer #6). Optional —
    // it doesn't exist until the first run after history tracking shipped.
    let history = null;
    try {
      const hres = await fetch("history.json?" + Date.now());
      if (hres.ok) history = await hres.json();
    } catch (e) { /* no history yet */ }
    const runs = (history && history.runs) || [];
    const scoreSeries = (name) =>
      runs.map((r) => (r.projects && r.projects[name] ? r.projects[name].score : null));

    $("generated").textContent =
      "Last run: " + new Date(d.generated).toLocaleString() + " — " + (d.status || "");
    $("digest").innerHTML = formatDigest(d.summary || "");

    const c = d.counts || {};
    $("stats").innerHTML = [
      ["tools", "tool calls"],
      ["issues", "issues filed"],
      ["enhancements", "enhancements"],
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
      const chips = [
        `<span class="rc ok">${rollup.ok}/${rollup.total} healthy</span>`,
        att.length ? `<span class="rc warn">${att.length} need${att.length === 1 ? "s" : ""} attention</span>` : "",
        `<span class="rc">${rollup.issues} issue${rollup.issues === 1 ? "" : "s"} filed</span>`,
        `<span class="rc">${rollup.enhancements} idea${rollup.enhancements === 1 ? "" : "s"}</span>`,
      ].filter(Boolean).join("");
      // Only projects past the nudge threshold get an explicit call-out row.
      const nudges = att.filter((a) => a.nudge).map((a) => {
        const st = ["idle", "blind", "error"].includes(a.status) ? a.status : "blind";
        const badge = { idle: "IDLE", blind: "BLIND", error: "ERROR" }[st];
        return `<div class="nudge ${st}"><span class="pbadge ${st}">${badge}</span>
          <span class="ntext"><b>${escapeHtml(a.name)}</b> — ${escapeHtml(a.detail)}</span></div>`;
      }).join("");
      $("rollup").innerHTML = `<div class="rollup-chips">${chips}</div>` +
        (nudges ? `<div class="nudges">${nudges}</div>` : "");
      $("rollup-card").style.display = "";
    }

    // Per-project health with BLIND badges (blind-spot tracking)
    const projects = d.projects || {};
    const names = Object.keys(projects);
    if (names.length) {
      $("projects-card").style.display = "";
      $("projects").innerHTML = names.map((name) => {
        const p = projects[name];
        const st = ["ok", "idle", "error", "blind"].includes(p.status) ? p.status : "blind";
        const badge = { ok: "OK", idle: "IDLE", error: "ERROR", blind: "BLIND" }[st];
        const lastOk = p.last_ok ? " · last ok " + new Date(p.last_ok).toLocaleDateString() : " · never read";
        let meta, alert = "";
        if (st === "ok") {
          meta = "healthy";
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
        return `<div class="prow${alert}">
          <div><div class="pname">${escapeHtml(name)}</div>
            <div class="pmeta">${escapeHtml(meta)}</div></div>
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
      return `${header}<div class="item ${slug}">
        <div class="meta">${cat ? `<span class="chip ${known}">${escapeHtml(cat)}</span>` : ""}
          <span>${escapeHtml(t.ts)} · ${escapeHtml(name)}</span></div>
        <div class="body">${escapeHtml(t.text)}</div></div>`;
    }).join("");
  } catch (e) {
    $("generated").textContent = "No digest published yet.";
  }
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
// Per-run Copy buttons in the history log are rendered dynamically, so delegate.
$("history-log").addEventListener("click", (e) => {
  const btn = e.target.closest(".run-copy");
  if (!btn) return;
  const run = priorRuns[Number(btn.dataset.run)];
  if (run) copyRecord(run, btn);
});
registerSW();
loadDigest();
