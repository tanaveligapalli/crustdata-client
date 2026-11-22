"use strict";
/* Sponsor Agent page. Every value from the server is inserted as text (textContent), never as
   HTML: company names, quotes from posts and pages, and model-written pitches are untrusted. */

const $ = (selector) => document.querySelector(selector);

const state = {
  config: null,
  runId: null,
  view: null,
  debug: false,
  picked: new Set(),
  approved: new Set(),
  reviewedFailures: false,
  timer: null,
  rendered: {},          // what each list was last drawn from, so polling doesn't redraw it
};

const FOUND_BY_KEYS = {
  "Sponsored similar events": "past_sponsor", "Sponsored your past events": "host_past_sponsor",
  "Like your past sponsors": "lookalike", "Hiring brand & sponsorship roles": "hiring",
  "In your event's industries": "company_search",
};
const SOURCE_LABELS = {
  linkedin_post: "LinkedIn post", instagram_post: "Instagram post", web_fetch: "Web page",
  "crustdata:/job/search": "LinkedIn job post", "crustdata:/company/search": "Company record",
  "crustdata:/person/search": "People data", host_brief: "Your brief",
};
const COLUMNS = [
  { key: "drafted", title: "Drafted", states: ["drafted", "approved"] },
  { key: "sent", title: "Sent", states: ["sent", "follow_up_sent"] },
  { key: "replied", title: "In conversation", states: ["replied_question", "replied_interested", "negotiating"] },
  { key: "booked", title: "Meeting booked", states: ["meeting_booked"] },
  { key: "closed", title: "Closed", states: ["declined", "opted_out", "no_response"] },
  { key: "person", title: "Needs you", states: ["needs_person"] },
];
const HOST_TYPES = { university: "University", ngo: "NGO", company: "Company", community: "Community" };
const MONO_COLORS = ["#fecc4c", "#ef987c", "#d9e5ff", "#ffd6f6", "#dff5e8", "#fcf3d2"];
const ARROW = '<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>'; // static markup only

/* -- small helpers ----------------------------------------------------------------- */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function show(node, visible) { node.hidden = !visible; }

function safeUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch { return null; }
}

function rupees(amount) {
  if (amount === null || amount === undefined) return null;
  if (amount >= 10000000) return `₹${+(amount / 10000000).toFixed(2)} crore`;
  if (amount >= 100000) return `₹${+(amount / 100000).toFixed(2)} lakh`;
  return `₹${amount.toLocaleString("en-IN")}`;
}

function niceDate(iso) {
  if (!iso) return null;
  const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
  return isNaN(d) ? iso : d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

function initials(name) {
  const words = (name || "?").replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
  return (words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2)).toUpperCase();
}

function hashColor(text) {
  let h = 0;
  for (const ch of text || "") h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return MONO_COLORS[h % MONO_COLORS.length];
}

function plainDomain(domain) {
  return domain && /^[a-z0-9.-]+\.[a-z]{2,}$/i.test(domain) ? domain.toLowerCase() : null;
}

/* A logo: Crustdata's, then the site's icon (only if it's a real one), then initials. */
function logoEl(name, domain, url) {
  const box = el("span", { class: "logo", "aria-hidden": "true" });
  const sources = [];
  if (safeUrl(url)) sources.push({ src: url, minWidth: 1 });
  const d = plainDomain(domain);
  if (d) sources.push({ src: `https://www.google.com/s2/favicons?domain=${encodeURIComponent(d)}&sz=128`, minWidth: 48 });
  const monogram = () => {
    box.replaceChildren();
    box.classList.add("mono");
    box.style.background = hashColor(name);
    box.textContent = initials(name);
  };
  const next = () => {
    const item = sources.shift();
    if (!item) return monogram();
    const img = el("img", { alt: "", loading: "lazy", referrerpolicy: "no-referrer" });
    img.addEventListener("load", () => (img.naturalWidth >= item.minWidth ? null : next()));
    img.addEventListener("error", next);
    img.src = item.src;
    box.replaceChildren(img);
  };
  next();
  return box;
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  let data = {};
  try { data = await response.json(); } catch { /* empty body */ }
  return { status: response.status, ok: response.ok, data };
}

/* -- start-up ----------------------------------------------------------------------- */

async function init() {
  const { data: config } = await api("/api/config");
  state.config = config;

  if (config.debug_available) {
    const toggle = $("#debug-toggle");
    try { state.debug = localStorage.getItem("sponsor-debug") === "1"; } catch { state.debug = false; }
    if (/[#&]debug=1\b/.test(location.hash)) state.debug = true;   // a link straight into the operator view
    toggle.checked = state.debug;
    show($("#debug-switch"), true);
    toggle.addEventListener("change", () => {
      state.debug = toggle.checked;
      try { localStorage.setItem("sponsor-debug", state.debug ? "1" : "0"); } catch { /* private mode */ }
      state.rendered = {};
      if (state.runId) {
        history.replaceState(null, "", `#run=${state.runId}${state.debug ? "&debug=1" : ""}`);
        refresh();
      }
    });
  }
  show($("#replay-pill"), config.offline);
  $("#run-hint").textContent = config.offline
    ? "Replay mode: shows events that already ran online, at no cost. Drafting and sending are off."
    : `The first search takes a few minutes and is capped at ${config.credit_limit} credits. The same words again cost nothing.`;

  const examples = $("#examples");
  for (const example of config.examples || []) {
    const chip = el("button", { class: "chip", type: "button", "aria-pressed": "false", text: example.name });
    chip.addEventListener("click", () => {
      $("#description").value = example.text;
      examples.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
    });
    examples.append(chip);
  }

  drawSourcesStrip();
  loadRecent();
  $("#run-btn").addEventListener("click", startRun);
  $("#draft-btn").addEventListener("click", draftPitches);
  $("#send-btn").addEventListener("click", sendApproved);

  const match = location.hash.match(/^#run=([\w-]+)/);
  if (match) openRun(match[1]);
}

async function loadRecent() {
  const { ok, data } = await api("/api/runs");
  if (!ok || !data.length) return;
  const list = $("#recent-list");
  list.replaceChildren(...data.filter((r) => r.event_name).map((r) =>
    el("button", { class: "chip", type: "button", text: r.event_name, onclick: () => openRun(r.run_id) })));
  show($("#recent"), list.children.length > 0);
}

/* -- running ---------------------------------------------------------------------- */

async function startRun() {
  const description = $("#description").value.trim();
  if (!description) {
    showAlert("Describe your event first: name, date, city, audience, host and website.");
    $("#description").focus();
    return;
  }
  setBusy($("#run-btn"), true);
  const { ok, data } = await api("/api/runs", { method: "POST", body: JSON.stringify({ description }) });
  setBusy($("#run-btn"), false);
  if (!ok) return showAlert(data.error || "Couldn't start.");
  state.picked.clear();
  state.approved.clear();
  state.reviewedFailures = false;
  openRun(data.run_id);
  loadRecent();
}

function openRun(runId) {
  if (state.runId !== runId) {
    state.picked.clear();
    state.approved.clear();
    state.reviewedFailures = false;
    state.rendered = {};
    delete $("#failed").dataset.drawn;
  }
  state.runId = runId;
  history.replaceState(null, "", `#run=${runId}${state.debug ? "&debug=1" : ""}`);
  document.querySelectorAll("[data-needs-run]").forEach((n) => show(n, true));
  show($("#progress"), true);
  refresh();
  $("#progress").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function refresh() {
  clearTimeout(state.timer);
  const runId = state.runId;
  const { ok, status, data } = await api(`/api/runs/${encodeURIComponent(runId)}${state.debug ? "?debug=1" : ""}`);
  if (runId !== state.runId) return;                       // another run was opened meanwhile
  if (!ok) {
    showAlert(status === 404 ? "That run isn't on this screen any more." : data.error || "Couldn't load the run.");
    return;
  }
  state.view = data;
  render(data);
  if (data.job && data.job.running) state.timer = setTimeout(refresh, 2000);
}

async function action(kind, companyIds, button) {
  setBusy(button, true);
  const { ok, status, data } = await api(`/api/runs/${encodeURIComponent(state.runId)}/${kind}`, {
    method: "POST", body: JSON.stringify({ company_ids: [...companyIds] }),
  });
  setBusy(button, false);
  if (!ok && status !== 409) return showAlert(data.error || "Something went wrong.");
  if (!ok) showAlert(data.error);
  refresh();
}

function draftPitches() {
  if (!state.picked.size) return;
  action("pitches", state.picked, $("#draft-btn"));
}

function sendApproved() {
  const ids = [...state.approved];
  if (!ids.length) return;
  const where = state.config.email === "team_inbox" ? "the team inbox" : "the outbox/ folder on this machine";
  const names = state.view.pitches.filter((p) => state.approved.has(p.company_id)).map((p) => p.company).join(", ");
  if (!confirm(`Send ${ids.length} email${ids.length > 1 ? "s" : ""} (${names})?\n\nThey go to ${where}, exactly as shown, once each.`)) return;
  action("send", ids, $("#send-btn"));
}

function setBusy(button, busy) {
  button.disabled = busy;
  button.classList.toggle("is-busy", busy);
}

function showAlert(message, extra) {
  const box = $("#alert");
  box.replaceChildren(el("span", { text: message }), extra || "");
  show(box, Boolean(message));
}

/* -- drawing a run ------------------------------------------------------------------ */

function render(view) {
  const job = view.job || {};
  const running = Boolean(job.running);
  const sponsorsDone = view.total_found > 0;
  const stages = {
    brief: Boolean(view.brief), host: Boolean(view.host), sponsors: sponsorsDone,
    pitches: view.pitches.length > 0,
    pipeline: view.pipeline.some((r) => !["drafted", "approved"].includes(r.state)),
  };
  let activeSet = false;
  document.querySelectorAll("#progress li").forEach((li) => {
    const done = stages[li.dataset.stage];
    li.classList.toggle("done", done);
    const active = running && !done && !activeSet;
    if (active) activeSet = true;
    li.classList.toggle("active", active);
  });

  const status = $("#status");
  status.textContent = running ? job.running + "…" : "";
  show(status, running);

  if (job.error) {
    const canResume = !["incomplete", "unreadable", "budget"].includes(job.error.kind) && !sponsorsDone;
    showAlert(job.error.message, canResume ? resumeButton() : null);
  } else if (!running && view.brief && !sponsorsDone) {
    showAlert("This search stopped before it finished. Finished steps are kept, so resuming costs nothing for them.", resumeButton());
  } else {
    showAlert("");
  }

  show($("#results"), Boolean(view.brief));
  if (!view.brief) return;
  drawBrief(view);
  drawHost(view.host);
  drawDebugPanel(view);
  drawSponsors(view, running);
  drawPitches(view, running);
  drawPipeline(view);
  $("#footer-run").textContent = `Run ${view.run_id}`;
  if (view.sponsors.some((s) => s.logo || plainDomain(s.domain))) drawLogoStrip(view.sponsors);
}

function resumeButton() {
  return el("div", {}, el("button", {
    class: "chip", type: "button", text: "Resume", style: "margin-top:.6rem",
    onclick: (e) => action("search", [], e.currentTarget),
  }));
}

function drawBrief(view) {
  const b = view.brief;
  const rows = [
    ["Event", b.event_name],
    ["Type", [b.profile === "concert" ? "Concert / festival" : "Conference / summit",
              b.host_type ? `${HOST_TYPES[b.host_type] || b.host_type} host` : null].filter(Boolean).join(" · ")],
    ["When", niceDate(b.event_date)],
    ["Where", b.city],
    ["Audience", [b.audience_size ? `${b.audience_size.toLocaleString("en-IN")} people` : null, b.audience].filter(Boolean).join(" · ")],
    ["Host", [b.host_name, b.host_website].filter(Boolean).join(" · ")],
    ["Tiers", b.tiers.length ? b.tiers.map((t) => el("span", { class: "tier" }, el("b", { text: t.name }), rupees(t.price_inr) || "price on request")) : null],
    ["Past sponsors", b.past_sponsors.join(", ")],
    ["Current", b.current_sponsors.join(", ")],
    ["Avoid", b.excluded_categories.join(", ")],
  ];
  $("#brief-facts").replaceChildren(...rows.filter(([, v]) => v && v.length).flatMap(([k, v]) =>
    [el("dt", { text: k }), el("dd", {}, v)]));

  const dropped = view.dropped || [];
  show($("#dropped"), dropped.length > 0);
  $("#dropped summary").textContent = `${dropped.length} detail${dropped.length > 1 ? "s" : ""} left out: not found word for word in your description`;
  $("#dropped-list").replaceChildren(...dropped.map((d) => el("li", { text: `${d.field.replace(/_/g, " ")}: ${d.reason}` })));
}

function drawHost(host) {
  const body = $("#host-body");
  if (!host) return body.replaceChildren(el("p", { class: "small", text: "Checking…" }));
  const ok = host.status === "verified";
  const link = safeUrl(host.source_url);
  body.replaceChildren(el("div", { class: `host-badge ${ok ? "host-ok" : "host-no"}` },
    el("span", { class: "host-icon", text: ok ? "✓" : "!" }),
    el("div", {},
      el("p", {}, el("strong", { text: ok ? "Verified." : "Not verified." }),
        ok ? " Your website names you, so outreach can go ahead." : ` ${host.missing || ""} Outreach stays blocked until this is fixed.`),
      el("p", { class: "src" }, link ? el("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: link }) : host.source_url || "",
        host.date ? ` · ${niceDate(host.date)}` : ""))));
}

function drawDebugPanel(view) {
  const on = state.debug && Boolean(view.debug);
  show($("#debug-panel"), on);
  if (!on) return;
  const d = view.debug;
  $("#debug-host-type").textContent = HOST_TYPES[d.host_type] || d.host_type;
  const max = Math.max(...Object.values(d.weights), 0.0001);
  $("#debug-weights").replaceChildren(...Object.entries(d.weights).flatMap(([name, w]) => [
    el("span", { text: name.replace(/_/g, " ") }),
    el("span", { class: "bar" }, el("span", { style: `width:${(w / max) * 100}%` })),
    el("span", { class: "num", text: w.toFixed(2) }),
  ]));
  $("#debug-cost").textContent = `${d.cost.credits} credits · $${d.cost.model_usd.toFixed(3)} model`;
}

function drawSponsors(view, running) {
  const failed = view.failed_pools || [];
  const failBox = $("#failed");
  show(failBox, failed.length > 0);
  if (failed.length && !failBox.dataset.drawn) {
    const check = el("input", { type: "checkbox" });
    check.checked = state.reviewedFailures;
    check.addEventListener("change", () => { state.reviewedFailures = check.checked; updatePickBar(); });
    failBox.replaceChildren(
      el("span", { text: `Some sources didn't answer, so this list may be incomplete: ${failed.join(", ")}.` }),
      el("label", {}, check, "A person has looked at this. Continue anyway."));
    failBox.dataset.drawn = "1";
  }

  const shown = view.sponsors.length;
  $("#sponsors-sub").textContent = shown
    ? `${view.total_found} companies found. The best ${shown}, in order; tap one to see every source.`
    : view.total_found ? "Every company found was excluded (see below)." : "";

  const signature = JSON.stringify([state.debug, view.sponsors.map((s) => [s.company_id, s.logo, s.debug && s.debug.evidence_score])]);
  if (state.rendered.sponsors !== signature) {
    state.rendered.sponsors = signature;
    $("#sponsor-list").replaceChildren(...view.sponsors.map((s, i) => sponsorCard(s, i)));
  }

  const excluded = view.excluded || [];
  show($("#excluded"), excluded.length > 0);
  $("#excluded summary").textContent = `${excluded.length} left out on purpose`;
  $("#excluded-list").replaceChildren(...excluded.map((x) => el("li", {}, el("strong", { text: x.name }), `: ${x.reason}`)));

  show($("#pick-bar"), shown > 0 && !state.config.offline);
  $("#draft-btn").dataset.running = running ? "1" : "";
  updatePickBar();
}

function sponsorCard(s, index) {
  const card = el("li", { class: "sponsor" + (state.picked.has(s.company_id) ? " is-picked" : ""), style: `animation-delay:${Math.min(index, 12) * 35}ms` });
  const verified = s.facts.filter((f) => f.status === "verified");
  const top = verified[0];
  const evidenceList = el("ul", { class: "evidence", hidden: true }, s.facts.map(factItem));
  const factsButton = s.facts.length ? el("button", {
    class: "facts-toggle", type: "button", "aria-expanded": "false",
    text: `${s.verified_facts} sourced fact${s.verified_facts === 1 ? "" : "s"}${s.facts.length > s.verified_facts ? ` · ${s.facts.length - s.verified_facts} we couldn't verify` : ""}`,
    onclick: (e) => {
      const open = evidenceList.hidden;
      evidenceList.hidden = !open;
      e.currentTarget.setAttribute("aria-expanded", String(open));
    },
  }) : null;

  const pick = el("input", { type: "checkbox", "aria-label": `Pick ${s.name}` });
  pick.checked = state.picked.has(s.company_id);
  pick.addEventListener("change", () => {
    if (pick.checked) state.picked.add(s.company_id); else state.picked.delete(s.company_id);
    card.classList.toggle("is-picked", pick.checked);
    updatePickBar();
  });

  card.append(
    el("span", { class: "rank", text: String(s.position) }),
    logoEl(s.name, s.domain, s.logo),
    el("div", { class: "sponsor-main" },
      el("p", { class: "sponsor-name", text: s.name }),
      el("p", { class: "sponsor-meta", text: [plainDomain(s.domain), s.industries.join(", ")].filter(Boolean).join(" · ") }),
      el("div", { class: "tags" }, s.found_by.map((label) => el("span", { class: `tag tag-${FOUND_BY_KEYS[label] || "other"}`, text: label }))),
      top ? el("p", { class: "why", text: top.claim }) : el("p", { class: "small", text: "Found by search; no sourced reason yet." }),
      factsButton, evidenceList),
    state.config.offline ? el("span") : el("label", { class: "pick" }, pick, "Pitch"),
  );
  if (state.debug && s.debug) card.append(debugBox(s.debug));
  return card;
}

function factItem(f) {
  const ok = f.status === "verified";
  const link = safeUrl(f.source_url);
  const source = SOURCE_LABELS[f.source_type] || f.source_type;
  return el("li", { class: ok ? "" : "unverified" },
    el("span", { class: `status-tag ${ok ? "ok" : "no"}`, text: ok ? "Verified" : "Can't verify" }),
    el("span", { class: "claim", text: f.claim }),
    f.quote ? el("span", { class: "quote", text: `“${f.quote}”` }) : null,
    el("span", { class: "src" },
      link ? el("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: source }) : source,
      f.date ? ` · ${niceDate(f.date)}` : "",
      !ok && f.missing ? ` · missing: ${f.missing}` : ""));
}

function debugBox(d) {
  const pct = (x) => (x === null || x === undefined ? "–" : x.toFixed(2));
  return el("div", { class: "debug-box" },
    el("div", { class: "score-row" },
      el("span", {}, "rank value ", el("b", { text: pct(d.evidence_score) })),
      el("span", {}, "score ", el("b", { text: pct(d.score) })),
      el("span", {}, "confidence ", el("b", { text: pct(d.confidence) }))),
    el("table", { class: "signals" },
      el("thead", {}, el("tr", {}, ["Signal", "Weight", "Value", "Adds"].map((h) => el("th", { text: h })))),
      el("tbody", {}, d.signals.filter((r) => r.weight > 0 || r.known).map((r) =>
        el("tr", { class: r.known ? "" : "unknown" },
          el("td", { text: r.label }),
          el("td", { text: r.weight.toFixed(2) }),
          el("td", {}, r.known ? [el("span", { class: "bar" }, el("span", { style: `width:${r.value * 100}%` })), r.value.toFixed(2)] : "unknown"),
          el("td", { text: r.known ? r.contribution.toFixed(3) : "–" }))))));
}

function updatePickBar() {
  const needsReview = (state.view?.failed_pools || []).length > 0 && !state.reviewedFailures;
  const running = $("#draft-btn").dataset.running === "1";
  $("#pick-count").textContent = String(state.picked.size);
  $("#draft-btn").disabled = !state.picked.size || needsReview || running;
}

function drawPitches(view, running) {
  const where = state.config.email === "team_inbox" ? "the team inbox" : "outbox/ on this machine (Gmail isn't set up)";
  $("#mail-dest").textContent = `Approved emails go to ${where}.`;
  show($("#pitches"), view.pitches.length > 0);
  const states = Object.fromEntries(view.pipeline.map((r) => [r.company_id, r.state]));

  const signature = JSON.stringify(view.pitches.map((p) => [p.company_id, p.subject, states[p.company_id]]));
  if (state.rendered.pitches !== signature) {
    state.rendered.pitches = signature;
    $("#pitch-list").replaceChildren(...view.pitches.map((p) => pitchCard(p, states[p.company_id] || "drafted")));
  }
  for (const id of [...state.approved]) if ((states[id] || "drafted") !== "drafted") state.approved.delete(id);
  $("#approve-count").textContent = String(state.approved.size);
  $("#send-btn").disabled = !state.approved.size || running || state.config.offline;
}

function pitchCard(p, pitchState) {
  const card = el("article", { class: "pitch" + (state.approved.has(p.company_id) ? " is-approved" : "") });
  let foot;
  if (pitchState === "drafted") {
    const box = el("input", { type: "checkbox", "aria-label": `Approve the pitch to ${p.company}` });
    box.checked = state.approved.has(p.company_id);
    box.addEventListener("change", () => {
      if (box.checked) state.approved.add(p.company_id); else state.approved.delete(p.company_id);
      card.classList.toggle("is-approved", box.checked);
      $("#approve-count").textContent = String(state.approved.size);
      $("#send-btn").disabled = !state.approved.size || state.config.offline;
    });
    foot = el("label", { class: "pick" }, box, "Approve");
  } else {
    foot = el("span", { class: "state-pill", text: pitchState.replace(/_/g, " ") });
  }
  card.append(
    el("div", { class: "pitch-head" },
      el("p", {}, el("span", { class: "label", text: "To" }), p.to_role),
      el("p", {}, el("span", { class: "label", text: "Company" }), p.company)),
    el("p", { class: "pitch-subject", text: p.subject }),
    el("pre", { class: "pitch-body", text: p.body }),
    p.notes && p.notes.length ? el("p", { class: "pitch-notes", text: p.notes.join(" · ") }) : null,
    el("div", { class: "pitch-foot" }, el("span", { class: "small", text: "Sent once, exactly as shown" }), foot));
  return card;
}

function drawPipeline(view) {
  const rows = view.pipeline;
  show($("#pipeline"), rows.length > 0);
  $("#reply-cmd").textContent = `python outreach.py replies ${view.run_id} brief.json`;
  $("#board").replaceChildren(...COLUMNS.map((col) => {
    const deals = rows.filter((r) => col.states.includes(r.state));
    return el("div", { class: `column col-${col.key}` },
      el("div", { class: "column-head" }, el("span", { text: col.title }), el("span", { class: "count", text: String(deals.length) })),
      el("div", { class: "column-body" }, deals.length
        ? deals.map((r) => el("div", { class: "deal" }, el("b", { text: r.company }), el("span", { text: r.why || r.state.replace(/_/g, " ") })))
        : el("p", { class: "empty", text: "Nothing yet" })));
  }));
}

/* -- the scrolling strip ------------------------------------------------------------ */

function drawSourcesStrip() {
  const sources = [
    ["◎", "LinkedIn sponsor posts"], ["✦", "Instagram event posts"], ["▲", "LinkedIn job posts"],
    ["■", "Company records"], ["●", "Company websites"], ["◆", "CSR reports"], ["✚", "Alumni data"],
  ];
  fillMarquee(() => sources.map(([icon, label]) =>
    el("div", { class: "marquee-cell" }, el("span", { class: "source-icon", "aria-hidden": "true", text: icon }), label)));
}

function drawLogoStrip(sponsors) {
  const signature = JSON.stringify(sponsors.map((s) => [s.company_id, s.logo]));
  if (state.rendered.strip === signature) return;
  state.rendered.strip = signature;
  $("#strip-title").textContent = "Companies we found for your event";
  fillMarquee(() => sponsors.slice(0, 16).map((s) =>
    el("div", { class: "marquee-cell" }, logoEl(s.name, s.domain, s.logo), s.name)));
}

function fillMarquee(makeCells) {
  // Two copies side by side, so the loop scrolls by half its width without a seam. Each copy
  // is built fresh: a cloned logo would lose its fallback (site icon, then initials).
  const copy = makeCells();
  copy.forEach((n) => n.setAttribute("aria-hidden", "true"));
  $("#marquee-track").replaceChildren(...makeCells(), ...copy);
}

init().catch((err) => {
  showAlert("Couldn't reach the server. Is `python web/server.py` running?");
  console.error(err);
});
