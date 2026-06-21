// Project Overseer dashboard — fetches the latest digest the weekly run
// committed, renders it readably, and lets you opt into push notifications.

const $ = (id) => document.getElementById(id);

function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

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

async function loadDigest() {
  try {
    const res = await fetch("digest.json?" + Date.now()); // bust cache
    if (!res.ok) throw new Error(res.status);
    const d = await res.json();

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
          meta = "no recent activity" + ((p.idle_cycles || 0) >= 2 ? ` · idle ${p.idle_cycles} cycles` : "");
          if ((p.idle_cycles || 0) >= 2) alert = " alert";
        } else if (st === "error") {
          meta = p.reason || "read failed";
          alert = " alert";
        } else {
          meta = (p.reason || "no data") + ((p.blind_cycles || 0) >= 2 ? ` · blind ${p.blind_cycles} cycles` : "") + lastOk;
          if ((p.blind_cycles || 0) >= 2) alert = " alert";
        }
        return `<div class="prow${alert}">
          <div><div class="pname">${escapeHtml(name)}</div>
            <div class="pmeta">${escapeHtml(meta)}</div></div>
          <span class="pbadge ${st}">${badge}</span></div>`;
      }).join("");
    }

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
registerSW();
loadDigest();
