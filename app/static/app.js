// SeasonSaathi UI (SPEC section 9). Vanilla JS, no build step. The UI only DISPLAYS what the API returns:
// no facts, KPIs or decisions are computed here. All API paths are relative (works unchanged on Cloud Run).
"use strict";

const S = {
  lang: "en", t: {}, paths: new Set(), schemas: {}, whoami: null, cfg: null, villages: null, support: null,
  demo: false, results: {}, timers: [], charts: [], route: "",
};
const LANGS = [["en", "EN"], ["mr", "मराठी"], ["hi", "हिंदी"]];
const SPEECH = { en: "en-IN", mr: "mr-IN", hi: "hi-IN" };
const SIG = {
  ACT: { bg: "#2E7D32", fg: "#fff", icon: "✔" },
  WAIT: { bg: "#F9A825", fg: "#1a1a1a", icon: "⏸" },
  AVOID: { bg: "#C62828", fg: "#fff", icon: "✖" },
};
const LEVEL_COLOR = { HIGH: ["#C62828", "#fff"], MED: ["#F9A825", "#1a1a1a"], LOW: ["#2E7D32", "#fff"] };

// ---------- tiny helpers ----------

function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}

const clean = (kids) => kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false);
const put = (node, ...kids) => node.replaceChildren(...clean(kids));   // DOM methods do not flatten arrays or skip null
const add = (node, ...kids) => node.append(...clean(kids));

function T(path, fallback) {
  let v = S.t;
  for (const p of path.split(".")) v = v == null ? undefined : v[p];
  return v === undefined || v === null ? (fallback !== undefined ? fallback : path.split(".").pop()) : v;
}

function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { return null; }
}

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body || opts.form ? "POST" : "GET"), headers: {} };
  if (opts.form) init.body = opts.form;
  else if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const res = await fetch(path, init);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
  if (!res.ok) {
    const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : String(text).slice(0, 300);
    const err = new Error(`${res.status}: ${detail}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

const has = (path) => S.paths.has(path);
const optName = (n) => (n ? T("options." + n, n) : T("common.not_available"));
const fmtRs = (v) => (v === null || v === undefined ? T("common.not_available") : "₹" + Number(v).toLocaleString("en-IN"));
const fmtDate = (v) => (v ? new Date(v).toLocaleString(S.lang === "en" ? "en-IN" : S.lang + "-IN", { dateStyle: "medium", timeStyle: "short" }) : T("list.not_recorded"));
const isUnknown = (v) => v === null || v === undefined || v === "" || (typeof v === "number" && Number.isNaN(v));

function clearTimers() {
  S.timers.forEach((t) => clearInterval(t));
  S.timers = [];
  S.charts.forEach((c) => c.destroy());
  S.charts = [];
  if (window.speechSynthesis) speechSynthesis.cancel();
}

// ---------- small components ----------

function chip(text, bg, fg, extra) {
  return el("span", { class: "chip " + (extra || ""), style: { background: bg, color: fg } }, text);
}
const synthChip = () => chip(T("common.synthetic"), "#FFF3C4", "#5D4037", "border border-amber-400");
const realChip = () => chip(T("common.real"), "#E8F5E9", "#1B5E20", "border border-green-600");

function signalBadge(sig, big) {
  const s = SIG[sig];
  if (!s) return chip(T("common.not_available"), "#eee", "#333");
  return el("span", {
    class: "inline-flex items-center gap-2 rounded-xl " + (big ? "px-3 py-1" : "px-2 py-0.5 text-sm font-semibold"),
    style: { background: s.bg, color: s.fg }, role: "status",
  }, el("span", { "aria-hidden": "true", class: big ? "text-2xl" : "" }, s.icon),
    el("span", { class: big ? "signal-word" : "" }, T("signal." + sig, sig)));
}

function levelChip(level, prefix) {
  const c = LEVEL_COLOR[level] || ["#eee", "#333"];
  return chip((prefix ? prefix + ": " : "") + T("level." + level, level || T("common.not_available")), c[0], c[1]);
}

function confChip(conf) {
  return chip(T("f3.confidence") + ": " + T("confidence." + conf, conf), "#E3F2FD", "#01579B");
}

function riskBar(value) {
  // P5b B: Gemini's 0-100 judgement is not a probability; shown neutrally (no risk colour bands), used only as a tie-break.
  const label = T("f3.ai_score");
  if (value === null || value === undefined) return el("div", { class: "text-sm" }, label + ": " + T("common.not_available"));
  return el("div", { class: "text-xs text-gray-700", title: T("f3.ai_score_note") },
    `${label}: ${Math.round(Number(value))} · ${T("f3.ai_score_note")}`);
}

function banner(text, tone) {
  const tones = { warn: ["#FFF8E1", "#5D4037", "#F9A825"], info: ["#E3F2FD", "#01579B", "#0277BD"], danger: ["#FFEBEE", "#B71C1C", "#C62828"] };
  const [bg, fg, bd] = tones[tone || "warn"];
  return el("div", { class: "rounded-xl px-3 py-2 font-semibold text-sm", role: "note", style: { background: bg, color: fg, borderLeft: `6px solid ${bd}` } }, text);
}

function section(title, ...kids) {
  return el("section", { class: "card p-4 mt-4" }, el("h2", { class: "font-bold text-lg mb-2 text-primary" }, title), ...kids);
}

function button(label, onclick, kind, attrs) {
  const kinds = {
    primary: "bg-primary text-white hover:bg-green-900",
    accent: "bg-accent text-white hover:bg-sky-800",
    ghost: "bg-white text-primary border border-primary hover:bg-green-50",
    soil: "bg-soil text-white hover:bg-amber-900",
    danger: "bg-avoid text-white hover:bg-red-900",
  };
  return el("button", { type: "button", class: `tap rounded-xl px-4 py-2 font-semibold ${kinds[kind || "primary"]} disabled:opacity-50`, onclick, ...(attrs || {}) }, label);
}

function link(href, text) {
  return el("a", { href, target: "_blank", rel: "noopener noreferrer", class: "text-accent underline break-anywhere" }, text);
}

function errorCard(err, retry) {
  return el("div", { class: "card p-4 border-l-8", style: { borderLeftColor: "#C62828" }, role: "alert" },
    el("div", { class: "font-bold text-avoid" }, "⚠ " + T("common.error")),
    el("div", { class: "text-sm mt-1 break-anywhere" }, String(err && err.message ? err.message : err)),
    retry ? el("div", { class: "mt-3" }, button(T("common.retry"), retry, "primary")) : null);
}

function loader() {
  // Rotating generic text + elapsed seconds. NOT real steps: the real trace is shown after the response.
  const msgs = T("common.loading", ["…"]);
  const msg = el("div", { class: "font-semibold", "aria-live": "polite" }, msgs[0]);
  const secs = el("span", { class: "text-sm text-gray-700" }, "0 " + T("common.seconds"));
  const t0 = Date.now();
  const timer = setInterval(() => {
    const s = Math.floor((Date.now() - t0) / 1000);
    secs.textContent = s + " " + T("common.seconds");
    msg.textContent = msgs[Math.floor(s / 3) % msgs.length];
  }, 1000);
  S.timers.push(timer);
  const node = el("div", { class: "card p-4 mt-4" },
    el("div", { class: "flex items-center justify-between" }, msg, secs),
    el("div", { class: "skeleton h-24 mt-3" }), el("div", { class: "skeleton h-24 mt-3" }));
  return { node, stop: () => clearInterval(timer) };
}

async function withLoading(container, fn, render) {
  const l = loader();
  put(container, l.node);
  try {
    const data = await fn();
    l.stop();
    put(container);
    render(data);
  } catch (err) {
    l.stop();
    put(container, errorCard(err, () => withLoading(container, fn, render)));
  }
}

// ---------- facts, trace, explanation, banners, feedback (shared by F1-F4) ----------

function factChip(fid, scope) {
  if (!fid) return null;
  return el("button", {
    type: "button", class: "fact-chip ml-1 align-middle", "aria-label": `${T("trace.fact_id")} ${fid}`,
    onclick: (e) => { e.stopPropagation(); highlightFact(scope, fid); },
  }, fid);
}

function highlightFact(scope, fid) {
  const root = document.getElementById(scope);
  if (!root) return;
  const det = root.querySelector("details.facts-table");
  if (det) det.open = true;
  const row = root.querySelector(`[data-fact="${CSS.escape(fid)}"]`);
  if (!row) return;
  row.classList.remove("fact-flash");
  void row.offsetWidth;
  row.classList.add("fact-flash");
  row.scrollIntoView({ behavior: "smooth", block: "center" });
}

function driverLine(d, scope) {
  // Tapping the sentence highlights its fact row.
  return el("li", { class: "py-1 pl-3 border-l-4 border-green-100" },
    el("button", { type: "button", class: "text-left hover:underline", onclick: () => highlightFact(scope, d.fact_id) }, d.text),
    factChip(d.fact_id, scope));
}

function factsTable(facts, open) {
  if (!facts || !facts.length) return el("div", { class: "text-sm" }, T("trace.facts") + ": " + T("common.not_available"));
  const rows = facts.map((f) => el("tr", { "data-fact": f.fact_id, class: "border-t align-top" },
    el("td", { class: "p-2 font-mono text-xs break-anywhere" }, f.fact_id),
    el("td", { class: "p-2 text-sm break-anywhere" }, f.name ? el("div", { class: "font-semibold" }, f.name) : null, f.text,
      f.caveat ? el("div", { class: "mt-1" }, chip("⚠ " + f.caveat, "#FFF3C4", "#5D4037")) : null),
    el("td", { class: "p-2 text-sm whitespace-nowrap" }, isUnknown(f.value) ? T("common.unknown") : String(Array.isArray(f.value) ? f.value.join(", ") : f.value),
      f.unit ? el("div", { class: "text-xs text-gray-700" }, f.unit) : null),
    el("td", { class: "p-2 text-xs break-anywhere" }, f.source || T("common.not_available"),
      f.source_url ? el("div", {}, link(f.source_url, T("common.open_source"))) : null,
      f.is_synthetic ? el("div", { class: "mt-1" }, synthChip()) : null)));
  return el("details", { class: "facts-table mt-2", open: open || null },
    el("summary", { class: "tap flex items-center font-semibold text-accent" }, `🛰️ ${T("trace.show_facts")} (${facts.length})`),
    el("div", { class: "scroll-x" }, el("table", { class: "w-full text-left mt-2 min-w-[560px]" },
      el("thead", {}, el("tr", { class: "text-xs uppercase text-gray-700" },
        el("th", { class: "p-2" }, T("trace.fact_id")), el("th", { class: "p-2" }, T("trace.name")),
        el("th", { class: "p-2" }, T("trace.value")), el("th", { class: "p-2" }, T("common.source")))),
      el("tbody", {}, rows))));
}

function kv(label, value) {
  return el("div", { class: "text-sm" }, el("span", { class: "text-gray-700" }, label + ": "), el("span", { class: "font-semibold" }, value));
}

function stepBody(step, result, scope) {
  const d = step.detail || {};
  const list = (items, fn) => el("ul", { class: "list-disc ml-5 text-sm" }, (items || []).map((x) => el("li", {}, fn ? fn(x) : String(x))));
  switch (step.step) {
    case "planner":
      return el("div", {}, el("div", { class: "flex flex-wrap gap-1 mt-1" }, (d.tools || []).map((x) => chip(x, "#E8F5E9", "#1B5E20"))),
        d.reasoning ? el("details", { class: "mt-1 text-sm" }, el("summary", { class: "text-accent" }, T("common.details")),
          el("pre", { class: "whitespace-pre-wrap text-xs mt-1" }, d.reasoning)) : null);
    case "tool":
      return el("div", { class: "text-sm" }, (d.fact_ids || []).map((f) => factChip(f, scope)),
        d.cached ? el("span", { class: "ml-2 text-xs text-gray-700" }, "(cached)") : null);
    case "facts":
      return factsTable(result.facts, false);
    case "assessor":
      return d.options ? list(d.options, (o) => el("span", {}, optName(o.name || o), o.signal ? " · " + T("signal." + o.signal, o.signal) : "",
        o.confidence ? " · " + T("confidence." + o.confidence, o.confidence) : "")) : null;
    case "verifier_agent":
    case "verifier_code":
      return el("div", {}, d.attempt > 1 ? chip("↻ " + T("trace.regeneration") + " #" + d.attempt, "#FFF3C4", "#5D4037") : null,
        d.issues && d.issues.length ? list(d.issues) : null,
        d.dropped && d.dropped.length ? kv("dropped", d.dropped.map((x) => x.field || JSON.stringify(x)).join(", ")) : null,
        d.out_of_range && d.out_of_range.length ? kv("out of range", d.out_of_range.map((x) => `${x.field}=${x.value}`).join(", ")) : null,
        d.derived && d.derived.length ? kv(T("f1.derived"), d.derived.map((x) => (typeof x === "string" ? x : `${x.field}=${x.value} (${x.reason || ""})`)).join("; ")) : null);
    case "safety":
      return el("div", {}, d.notes ? list(d.notes) : null, d.rule ? kv(T("learning.rule"), d.rule) : null,
        d.risks ? kv(T("f4.risk"), Object.entries(d.risks).map(([k, v]) => `${optName(k)} ${v}`).join(" · ")) : null);
    case "calibration":
      return el("div", {}, d.rule ? kv(T("learning.rule"), d.rule) : null,
        d.options ? list(d.options, (o) => el("span", {}, optName(o.option), ": ", o.result || "", o.n !== undefined ? ` (n=${o.n})` : "", factChip(o.fact_id, scope)))
          : d.changes ? list(d.changes) : null);
    case "inject":   // demo: the corrupted claim must be impossible to miss
      return el("div", { class: "mt-1 rounded-xl p-3 font-semibold", role: "note", style: { background: "#FFEBEE", color: "#B71C1C", border: "2px solid #C62828" } },
        "💉 " + (d.change || T("common.not_available")));
    case "evidence":
      return null;
    case "usage":
      return d.calls && d.calls.length ? el("details", { class: "text-xs mt-1" }, el("summary", { class: "text-accent" }, T("common.details")),
        list(d.calls, (c) => `${c.role} · ${c.engine || ""} · ${c.backend}/${c.model} · ${c.cached ? "cache" : (c.latency_ms ?? "?") + " ms"} · ${c.tokens_in ?? "?"}/${c.tokens_out ?? "?"} tok`)) : null;
    case "stress":
      return d.crossed && d.crossed.length ? list(d.crossed) : null;
    case "writer":
      return el("div", { class: "text-sm whitespace-pre-line" }, d.text_mr || "", (d.fact_ids || []).map((f) => factChip(f, scope)));
    case "publish":
      return kv(T("f2.via"), d.via || T("common.not_available"));
    case "extractor":
      return d.fields ? list(d.fields, (f) => el("span", {}, `${f.field} = ${f.value}`, f.evidence ? el("span", { class: "text-gray-700" }, ` “${f.evidence}”`) : null)) : null;
    case "terms":
      return el("div", { class: "text-sm" }, d.terms_yaml && d.terms_yaml.length ? kv("terms.yaml", d.terms_yaml.map((x) => x.term ? `${x.term} → ${x.canonical || x.meaning || ""}` : JSON.stringify(x)).join("; ")) : null,
        d.gemini_normalised_terms && d.gemini_normalised_terms.length ? kv("Gemini", d.gemini_normalised_terms.map((x) => `${x.term} → ${x.meaning}`).join("; ")) : null);
    case "completeness":
      return el("div", { class: "text-sm" }, d.flags && d.flags.length ? kv(T("f1.flags"), d.flags.join(", ")) : null,
        d.next ? kv("next", typeof d.next === "object" ? Object.values(d.next).filter(Boolean).join(" / ") : String(d.next)) : null);
    case "interviewer":
      return d.question ? el("div", { class: "text-sm" }, d.question) : null;
    case "transcribe":
      return d.transcript ? el("div", { class: "text-sm" }, "“" + d.transcript + "”") : null;
    default:
      return Object.keys(d).length ? el("details", { class: "text-xs" }, el("summary", { class: "text-accent" }, T("common.details")),
        el("pre", { class: "whitespace-pre-wrap break-anywhere" }, JSON.stringify(d, null, 1))) : null;
  }
}

function engineBadge(result) {
  const o = result.orchestrator_used;
  let label = o, bg = "#455A64";
  if (o === "adk") { label = result.genai_backend === "vertex" ? T("trace.adk") : "ADK (" + (result.genai_backend || "?") + ")"; bg = "#1B5E20"; }
  else if (o === "plain") { label = result.fallback_reason ? T("trace.plain_fallback") : T("trace.plain"); bg = "#0277BD"; }
  else if (o === "cache") { label = T("trace.cache"); bg = "#6D4C41"; }
  else if (o === "rule") { label = T("trace.rule"); bg = "#6D4C41"; }
  return el("div", { class: "flex flex-wrap items-center gap-2 mt-3" },
    el("span", { class: "chip text-white", style: { background: bg, fontSize: "14px", padding: "6px 12px" } }, "⚙️ " + T("trace.engine") + ": " + (label || T("common.not_available"))),
    result.gemini_model ? chip(result.gemini_model + (result.genai_backend ? " · " + result.genai_backend : ""), "#ECEFF1", "#263238") : null,
    result.latency_ms !== undefined && result.latency_ms !== null ? chip(T("trace.latency") + ": " + (result.latency_ms / 1000).toFixed(1) + " s", "#ECEFF1", "#263238") : null,
    result.fallback_reason ? el("div", { class: "w-full text-sm text-soil" }, T("trace.fallback_reason") + ": " + result.fallback_reason) : null);
}

function traceTimeline(result, scope) {
  const steps = (result.trace || []).filter((s) => s.step !== "engine");
  const items = steps.map((s) => {
    const fail = /FAIL/.test(s.title) || s.step === "inject";
    return el("li", { class: "relative pl-6 pb-3 border-l-2 " + (fail ? "border-avoid" : "border-green-700") },
      el("span", { class: "absolute -left-[7px] top-1 w-3 h-3 rounded-full", style: { background: fail ? "#C62828" : "#1B5E20" }, "aria-hidden": "true" }),
      el("div", { class: "font-semibold text-sm break-anywhere" }, s.title),
      el("div", { class: "text-xs text-gray-700" }, [s.backend, s.model, s.ms !== null && s.ms !== undefined ? s.ms + " ms" : null].filter(Boolean).join(" · ")),
      stepBody(s, result, scope));
  });
  const hasFactsStep = steps.some((s) => s.step === "facts");
  return el("details", { class: "card p-4 mt-4", open: true },
    el("summary", { class: "tap flex items-center justify-between" },
      el("h2", { class: "font-bold text-lg text-primary" }, "🧭 " + T("trace.title")), el("span", { class: "text-xs text-gray-700" }, T("trace.tap_hint"))),
    el("ol", { class: "mt-3" }, items),
    !hasFactsStep && result.facts && result.facts.length ? factsTable(result.facts, false) : null,
    engineBadge(result));
}

function resultBanners(r) {
  const notes = [...(r.notes || []), ...((r.explain && r.explain.confidence_reason) || [])].join(" | ");
  return el("div", { class: "flex flex-col gap-2 mt-3" },
    r.withheld ? banner("⛔ " + T("banner.withheld") + (r.withheld_reason ? ": " + r.withheld_reason : ""), "danger") : null,
    r.safety_rule_applied || /Safety rule applied/i.test(notes) ? banner("🛡️ " + T("banner.safety"), "danger") : null,
    /profile incomplete/i.test(notes) ? banner("📋 " + T("banner.incomplete"), "warn") : null,
    /NDVI\) unavailable/i.test(notes) ? banner("🛰️ " + T("banner.ndvi"), "info") : null,
    /calibration downgrade/i.test(notes) ? banner("📊 " + T("banner.calibration"), "warn") : null,
    r.orchestrator_used === "cache" ? banner(T("banner.cached"), "info") : null,
    r.orchestrator_used === "rule" ? banner(T("banner.rule"), "warn") : null,
    /LLM_OFFLINE/.test(r.fallback_reason || "") ? banner(T("banner.offline"), "info") : null,
    (r.notes || []).filter((n) => /^Demo flag .* not applied/.test(n)).map((n) => banner("🎬 " + n, "danger")));
}

function pickVoice(lang) {
  // Use a voice that is really installed; Marathi falls back to a Hindi voice (same Devanagari script).
  const voices = speechSynthesis.getVoices();
  const find = (code) => voices.find((v) => v.lang.replace("_", "-").toLowerCase().startsWith(code));
  return find(SPEECH[lang].toLowerCase()) || find(lang) || (lang === "mr" ? find("hi") : null);
}

function speakButton(text, lang) {
  if (!window.speechSynthesis || !text) return null;
  lang = lang || (/[\u0900-\u097F]/.test(text) ? (S.lang === "en" ? "mr" : S.lang) : "en");
  const msg = el("span", { class: "text-xs text-soil ml-2", "aria-live": "polite" });
  let playing = false;   // own state: Chrome's speechSynthesis.speaking can stay stuck at true
  const label = () => { playing = false; b.textContent = "🔊 " + T("common.read_aloud"); };
  const b = button("🔊 " + T("common.read_aloud"), () => {
    speechSynthesis.cancel();   // clears a stuck queue from an earlier failed utterance
    if (playing) { label(); msg.textContent = ""; return; }
    const voice = pickVoice(lang);
    if (!voice) {   // browsers stay silent without a matching voice: say so instead
      msg.textContent = T("common.no_voice") + ` (${SPEECH[lang]})`;
      return;
    }
    const u = new SpeechSynthesisUtterance(text);
    u.voice = voice;
    u.lang = voice.lang;
    u.onstart = () => { msg.textContent = (voice.lang.toLowerCase().startsWith(lang) ? "🔊 " : `${T("common.voice_fallback")}: `) + voice.name; };
    u.onend = () => { label(); };
    u.onerror = (e) => { label(); msg.textContent = "⚠ " + (e.error || T("common.error")) + " · " + voice.name; };
    S.utterance = u;   // keep a reference: Chrome can garbage-collect a running utterance
    playing = true;
    b.textContent = "⏹ " + T("common.stop");
    msg.textContent = "… " + voice.name;
    speechSynthesis.speak(u);
    speechSynthesis.resume();   // Chrome sometimes starts paused
  }, "ghost", { "aria-label": T("common.read_aloud") });
  return el("span", { class: "inline-flex items-center flex-wrap" }, b, msg);
}

function explainPanel(r, scope) {
  const e = r.explain || {};
  const plainText = S.lang === "hi" ? (e.plain_explanation_hi || r.explanation_hi) : S.lang === "mr" ? (e.plain_explanation_mr || r.explanation_mr) : null;
  const plainBlocks = S.lang === "en"
    ? [["मराठी", e.plain_explanation_mr || r.explanation_mr, "mr"], ["हिंदी", e.plain_explanation_hi || r.explanation_hi, "hi"]].filter(([, t]) => t)
    : plainText ? [[null, plainText, S.lang]] : [];
  const wwc = e.what_would_change;
  return section("💬 " + T("explain.title"),
    el("h3", { class: "font-semibold mt-1" }, T("explain.plain")),
    plainBlocks.length ? plainBlocks.map(([label, text, lang]) => el("div", { class: "mt-1" }, label ? el("div", { class: "text-xs text-gray-700" }, label) : null,
      el("p", { class: "text-base leading-relaxed" }, text), el("div", { class: "mt-1" }, speakButton(text, lang))))
      : el("p", { class: "text-sm text-gray-700" }, T("explain.plain_missing")),
    e.why_recommended && e.why_recommended.length ? [el("h3", { class: "font-semibold mt-3" }, T("explain.why")),
      el("ul", { class: "flex flex-col gap-1" }, e.why_recommended.map((d) => driverLine(d, scope)))] : null,
    e.why_not_others && e.why_not_others.length ? [el("h3", { class: "font-semibold mt-3" }, T("explain.why_not")),
      el("ul", { class: "list-disc ml-5" }, e.why_not_others.map((w) => el("li", { class: "py-1" },
        el("span", { class: "font-semibold" }, optName(w.option) + ": "),
        el("button", { type: "button", class: "text-left hover:underline", onclick: () => highlightFact(scope, w.fact_id) }, w.text), factChip(w.fact_id, scope))))] : null,
    wwc ? el("div", { class: "mt-3 rounded-xl p-3", style: { background: "#E3F2FD", borderLeft: "6px solid #0277BD" } },
      el("div", { class: "font-semibold text-accent" }, "🔁 " + T("explain.what_would_change")),
      el("p", { class: "mt-1" }, wwc.text || T("common.not_available"), factChip(wwc.fact_id, scope))) : null,
    e.confidence_reason && e.confidence_reason.length ? [el("h3", { class: "font-semibold mt-3" }, T("explain.confidence_reason")),
      el("ul", { class: "list-disc ml-5 text-sm" }, e.confidence_reason.map((x) => el("li", {}, x)))] : null,
    e.data_gaps && e.data_gaps.length ? el("details", { class: "mt-3" }, el("summary", { class: "tap flex items-center font-semibold" }, `${T("explain.data_gaps")} (${e.data_gaps.length})`),
      el("ul", { class: "list-disc ml-5 text-sm" }, e.data_gaps.map((x) => el("li", {}, x)))) : null,
    e.evidence && e.evidence.text ? el("div", { class: "mt-3 text-sm text-gray-700" }, "📚 " + e.evidence.text) : null);
}

function feedbackBar(r, feature, hhId, rerun, learningDemo) {
  const status = el("div", { class: "text-sm mt-2", "aria-live": "polite" });
  const note = el("input", { type: "text", class: "border rounded-lg px-3 py-2 w-full mt-2 tap", placeholder: T("feedback.note"), "aria-label": T("feedback.note") });
  const rate = async (rating) => {
    try { await api("/api/feedback", { body: { request_id: r.request_id, rating, note: note.value || null } }); status.textContent = "✓ " + T("feedback.thanks"); }
    catch (err) { status.textContent = "⚠ " + err.message; }
  };
  const season = el("input", { type: "text", class: "border rounded-lg px-3 py-2 tap w-full", "aria-label": T("feedback.season"), placeholder: T("feedback.season") });
  const result = el("select", { class: "border rounded-lg px-3 py-2 tap w-full", "aria-label": T("feedback.result") },
    ((S.cfg && S.cfg.outcome_results) || ["good", "partial", "loss"]).map((x) => el("option", { value: x }, T("feedback." + x, x))));
  const cattle = el("input", { type: "number", min: "0", class: "border rounded-lg px-3 py-2 tap w-full", "aria-label": T("feedback.cattle_sold"), placeholder: T("feedback.cattle_sold") });
  const distress = el("input", { type: "checkbox", class: "w-6 h-6", id: "distress-" + r.request_id });
  const outStatus = el("div", { class: "text-sm mt-2", "aria-live": "polite" });
  const saveOutcome = async () => {
    try {
      await api("/api/outcomes", { body: { hh_id: hhId, request_id: r.request_id, season: season.value || "unspecified", actual_result: result.value,
        cattle_sold: cattle.value === "" ? null : Number(cattle.value), distress_sale: feature === "f4" ? distress.checked : null } });
      outStatus.textContent = "✓ " + T("feedback.outcome_saved");
    } catch (err) { outStatus.textContent = "⚠ " + err.message; }
  };
  return section("👍 " + T("feedback.title"),
    el("div", { class: "flex flex-wrap gap-2" },
      button("👍 " + T("feedback.up"), () => rate("up"), "primary", { "aria-label": T("feedback.up") }),
      button("👎 " + T("feedback.down"), () => rate("down"), "ghost", { "aria-label": T("feedback.down") }),
      button("↻ " + T("common.rerun"), rerun, "accent"),
      learningDemo ? button("🎓 " + T("feedback.learning_demo"), learningDemo, "soil", { title: T("feedback.learning_demo_hint"), id: "learning-demo-btn" }) : null),
    note, status,
    el("details", { class: "mt-3" }, el("summary", { class: "tap flex items-center font-semibold text-accent" }, "📝 " + T("feedback.outcome")),
      el("div", { class: "grid gap-2 sm:grid-cols-2 mt-2" }, season, result,
        feature === "f4" ? [cattle, el("label", { class: "flex items-center gap-2 tap", for: "distress-" + r.request_id }, distress, T("feedback.distress_sale"))] : null),
      el("div", { class: "mt-2" }, button(T("common.save"), saveOutcome, "primary")), outStatus),
    learningDemo ? el("div", { class: "text-xs text-gray-700 mt-2" }, T("feedback.learning_demo_hint")) : null);
}

// ---------- shell ----------

function renderShell() {
  document.documentElement.lang = S.lang;
  document.getElementById("app-name").textContent = T("app_name");
  document.getElementById("tagline").textContent = T("tagline");
  const toggle = document.getElementById("lang-toggle");
  put(toggle, ...LANGS.map(([code, label]) => el("button", {
    type: "button", class: "tap px-3 rounded-full text-sm font-semibold " + (S.lang === code ? "bg-white text-primary" : "text-white"),
    "aria-pressed": String(S.lang === code), onclick: () => setLang(code),
  }, label)));
  const tabs = [["households", "#/households"], ["new", "#/new"], ["watch", "#/watch"], ["learning", "#/learning"],
    has("/api/impact/latest") ? ["impact", "#/impact"] : null, ["about", "#/about"]].filter(Boolean);
  const cur = (location.hash || "#/households").split("/")[1] || "households";
  const active = { hh: "households", f3: "households", f4: "households", f6: "households" }[cur] || cur;
  document.getElementById("nav").replaceChildren(...tabs.map(([k, href]) => el("li", {},
    el("a", { href, class: "tap inline-flex items-center px-3 rounded-t-lg " + (active === k ? "bg-bg text-primary font-bold" : "text-white"),
      "aria-current": active === k ? "page" : null }, T("nav." + k)))));
  const w = S.whoami;
  document.getElementById("footer-inner").replaceChildren(w
    ? el("div", { class: "flex flex-wrap gap-x-4 gap-y-1" },
      el("span", {}, `${T("footer.genai")}: ${w.genai_backend}${w.location ? " (" + w.location + ")" : ""}`),
      el("span", {}, `${T("footer.model")}: ${w.gemini_model || T("common.not_available")}`),
      el("span", {}, `${T("footer.engine")}: ${w.orchestrator}`),
      el("span", {}, `${T("footer.data")}: ${w.data_backend}${w.bq_dataset ? " · " + w.bq_dataset : ""}`),
      el("span", {}, `storage: ${w.storage_backend} · alerts: ${w.alerts_mode}`))
    : el("span", {}, "whoami: " + T("common.not_available")));
  renderDemoBar();
}

function supportsForceFail() {
  const p = S.schemas.F3Request && S.schemas.F3Request.properties;
  const inject = p && p.inject;
  const enums = JSON.stringify(inject || {});
  return enums.includes("adk_fail");
}

function renderDemoBar() {
  const bar = document.getElementById("demo-bar");
  if (!S.demo) { bar.hidden = true; return; }
  bar.hidden = false;
  const steps = T("demo.steps", []);
  const targets = ["#/households", "#/f3/HH-DEMO-OK", "#/f3/HH-DEMO-OK?inject=bad_fact_id",
    supportsForceFail() ? "#/f3/HH-DEMO-OK?inject=adk_fail" : null, "#/f3/HH-DEMO-FLOOR", "#/f4/HH-DEMO-LIVESTOCK",
    "#/f3/HH-DEMO-OK?learn=1", "#/watch/PLOT-HH-DEMO-FLOOR", has("/api/impact/latest") ? "#/impact" : null];
  bar.className = "bg-soil text-white";
  put(bar, el("div", { class: "max-w-5xl mx-auto px-4 py-2 scroll-x" },
    el("div", { class: "flex items-center gap-2 whitespace-nowrap" },
      el("span", { class: "font-bold text-sm" }, "🎬 " + T("demo.bar")),
      steps.map((label, i) => targets[i]
        ? el("a", { href: targets[i] + (targets[i].includes("?") ? "&" : "?") + "run=" + Date.now(), class: "tap shrink-0 inline-flex items-center rounded-lg bg-white/15 hover:bg-white/25 px-2 text-xs" }, `${i + 1}. ${label}`)
        : el("span", { class: "shrink-0 inline-flex items-center rounded-lg bg-black/20 px-2 text-xs opacity-70", title: T("common.not_available") }, `${i + 1}. ${label} (${T("common.not_available")})`)))));
}

async function setLang(code) {
  S.lang = code;
  store("ss_lang", code);
  try { S.t = await api(`i18n/${code}.json`); } catch (e) { S.t = {}; }
  route(false);
}

// ---------- router ----------

function parseHash() {
  const raw = (location.hash || "#/households").slice(1);
  const [path, qs] = raw.split("?");
  return { parts: path.split("/").filter(Boolean), q: new URLSearchParams(qs || "") };
}

function route(fresh) {
  clearTimers();
  renderShell();
  const main = document.getElementById("main");
  const { parts, q } = parseHash();
  const [view, id] = parts;
  try {
    if (!view || view === "households") return viewHouseholds(main);
    if (view === "hh") return viewHub(main, id);
    if (view === "f3") return viewF3(main, id, q, fresh);
    if (view === "f4") return viewF4(main, id, q, fresh);
    if (view === "new") return viewF1(main);
    if (view === "watch") return id ? viewPlot(main, id) : viewWatch(main);
    if (view === "learning") return viewLearning(main);
    if (view === "impact") return viewImpact(main);
    if (view === "about") return viewAbout(main);
    put(main, errorCard("Unknown page: " + view, () => { location.hash = "#/households"; }));
  } catch (err) {
    put(main, errorCard(err, () => route(true)));
  }
}

function pageHeader(title, back, extra) {
  return el("div", { class: "flex items-center gap-2 flex-wrap" },
    back ? el("a", { href: back, class: "tap inline-flex items-center justify-center rounded-xl border border-primary text-primary px-3", "aria-label": T("common.back") }, "← " + T("common.back")) : null,
    el("h1", { class: "text-xl font-bold text-primary flex-1 break-anywhere" }, title), extra || null);
}

// ---------- 2. household list ----------

function viewHouseholds(main) {
  put(main, pageHeader(T("list.title")), el("p", { class: "text-sm text-gray-700" }, T("list.subtitle")));
  const box = el("div", { class: "mt-3" });
  add(main, box);
  withLoading(box, () => api("/api/households"), (rows) => {
    if (!rows.length) { add(box, el("div", { class: "card p-4" }, T("list.empty"))); return; }
    add(box, el("ul", { class: "grid gap-3 sm:grid-cols-2" }, rows.map((h) => el("li", {},
      el("a", { href: "#/hh/" + encodeURIComponent(h.hh_id), class: "card p-4 block hover:border-primary tap" },
        el("div", { class: "flex items-center justify-between gap-2" },
          el("span", { class: "font-bold break-anywhere" }, h.hh_id),
          el("span", { class: "flex gap-1 flex-wrap justify-end" }, h.is_demo ? chip("★ " + T("common.demo"), "#0277BD", "#fff") : null, h.is_synthetic ? synthChip() : realChip())),
        el("div", { class: "text-sm text-gray-700 mt-1" }, `${h.village || T("common.not_available")} · ${h.tenure ? T("tenure." + h.tenure, h.tenure) : T("common.unknown")}`),
        el("div", { class: "text-xs text-gray-700" }, `🗓️ ${T("list.created")}: ${fmtDate(h.created_at)}`),
        el("div", { class: "mt-2" }, el("div", { class: "flex justify-between text-xs" }, el("span", {}, T("list.completeness")), el("span", {}, h.completeness_pct + "%")),
          el("div", { class: "h-2 bg-gray-200 rounded-full" }, el("div", { class: "h-2 rounded-full", style: { width: h.completeness_pct + "%", background: h.flags.includes("incomplete") ? "#F9A825" : "#1B5E20" } }))),
        el("div", { class: "flex flex-wrap gap-2 mt-2 items-center" },
          h.latest_f3 && h.latest_f3.signal ? [signalBadge(h.latest_f3.signal, false), el("span", { class: "text-xs" }, optName(h.latest_f3.recommended))]
            : el("span", { class: "text-xs text-gray-700" }, T("list.no_run")),
          h.latest_alert ? levelChip(h.latest_alert.stress_level, "🔔 " + T("list.alert")) : null))))));
  });
}

// ---------- 3. household hub ----------

function profileCard(hh) {
  const val = (v, map) => (isUnknown(v) ? el("span", { class: "text-gray-700 italic" }, T("common.unknown")) : map ? T(map + "." + v, String(v)) : String(v));
  const water = hh.irrigation === "rainfed" && Number(hh.water_hours_per_week) === 0
    ? el("span", {}, "0 ", el("span", { class: "text-xs text-gray-700" }, "(" + T("hub.derived_rainfed") + ")")) : val(hh.water_hours_per_week);
  const rows = [["hub.tenure", val(hh.tenure, "tenure")], ["hub.cultivator", val(hh.primary_cultivator_gender, "gender")], ["hub.acres", val(hh.acres)],
    ["hub.irrigation", val(hh.irrigation, "irrigation")], ["hub.water_hours", water], ["hub.crops", val(hh.crops_last_season)],
    ["hub.cattle", val(hh.cattle_count)], ["hub.goats", val(hh.goats_count)], ["hub.fodder", val(hh.fodder_weeks_left)],
    ["hub.cash", val(hh.cash_buffer_band, "level")], ["hub.loan", val(hh.loan_due_month)], ["hub.off_farm", val(hh.off_farm_income)], ["hub.soil", val(hh.soil_type_farmer)]];
  return el("dl", { class: "grid grid-cols-2 gap-x-4 gap-y-2" }, rows.map(([k, v]) => el("div", {},
    el("dt", { class: "text-xs text-gray-700" }, T(k)), el("dd", { class: "font-semibold break-anywhere" }, v))));
}

function viewHub(main, hhId) {
  put(main, pageHeader(hhId, "#/households"));
  const box = el("div");
  add(main, box);
  withLoading(box, () => api("/api/households/" + encodeURIComponent(hhId)), (d) => {
    const hh = d.household;
    const plots = d.plots || [];
    add(box, 
      el("div", { class: "flex flex-wrap gap-2 mt-2" }, hh.is_synthetic ? synthChip() : realChip(),
        d.flags.includes("incomplete") ? chip("📋 " + T("banner.incomplete"), "#FFF8E1", "#5D4037") : null,
        chip(`${T("list.completeness")}: ${d.completeness_pct}%`, "#E8F5E9", "#1B5E20")),
      section("👪 " + T("hub.profile"),
        el("div", { class: "text-sm mb-2" }, `${T("hub.village")}: ${d.village ? `${d.village.name}, ${d.village.taluka}, ${d.village.district}` : T("common.not_available")}`,
          el("div", { class: "text-xs text-gray-700" }, `🗓️ ${T("list.created")}: ${fmtDate(hh.created_at)}`)),
        profileCard(hh),
        d.latest_f3 ? el("div", { class: "mt-3 flex items-center gap-2 flex-wrap text-sm" }, T("hub.latest") + ":", signalBadge(d.latest_f3.signal, false), optName(d.latest_f3.recommended)) : null),
      el("div", { class: "grid gap-3 sm:grid-cols-2 mt-4" },
        el("a", { href: `#/f3/${encodeURIComponent(hhId)}`, class: "tap card p-4 flex items-center gap-3 hover:border-primary" }, el("span", { class: "text-3xl", "aria-hidden": "true" }, "🌱"), el("span", { class: "font-bold text-lg" }, T("hub.f3"))),
        el("a", { href: `#/f4/${encodeURIComponent(hhId)}`, class: "tap card p-4 flex items-center gap-3 hover:border-primary" }, el("span", { class: "text-3xl", "aria-hidden": "true" }, "🐄"), el("span", { class: "font-bold text-lg" }, T("hub.f4"))),
        plots.map((p) => el("a", { href: `#/watch/${encodeURIComponent(p.plot_id)}`, class: "tap card p-4 flex items-center gap-3 hover:border-primary" },
          el("span", { class: "text-3xl", "aria-hidden": "true" }, "🛰️"), el("span", {}, el("span", { class: "font-bold text-lg block" }, T("hub.f2")), el("span", { class: "text-xs text-gray-700" }, p.plot_id)))),
        has("/api/f6/{hh_id}") ? el("a", { href: `#/f6/${encodeURIComponent(hhId)}`, class: "tap card p-4 flex items-center gap-3" }, "📄 " + T("hub.f6")) : null),
      plots.length ? null : el("div", { class: "mt-3" }, banner("🛰️ " + T("hub.no_plot"), "info")));
  });
}

// ---------- 4. F3 crop mix ----------

function demoControls(feature, hhId) {
  if (!S.demo) return null;
  return el("div", { class: "flex flex-wrap gap-2 mt-3 p-2 rounded-xl", style: { background: "#EFEBE9" } },
    el("span", { class: "font-semibold text-sm self-center" }, "🎬 " + T("demo.bar") + ":"),
    button("💉 " + T("demo.inject"), () => { location.hash = `#/${feature}/${encodeURIComponent(hhId)}?inject=bad_fact_id&run=${Date.now()}`; }, "soil"),
    supportsForceFail()
      ? button("💥 " + T("demo.force_fail"), () => { location.hash = `#/${feature}/${encodeURIComponent(hhId)}?inject=adk_fail&run=${Date.now()}`; }, "danger")
      : el("span", { class: "text-xs self-center" }, T("demo.force_fail") + ": " + T("common.not_available")));
}

function runFeature(main, feature, hhId, q, fresh, renderResult) {
  const inject = q.get("inject");
  const key = `${feature}:${hhId}:${inject || ""}:${q.get("run") || ""}`;
  const title = T(feature + ".title") + " · " + hhId;
  put(main, ...[pageHeader(title, "#/hh/" + encodeURIComponent(hhId)), demoControls(feature, hhId)].filter(Boolean));
  const box = el("div");
  add(main, box);
  const body = inject ? { inject } : {};
  const go = (force) => {
    if (!force && S.results[key]) { put(box); renderResult(box, S.results[key], go); return; }
    withLoading(box, () => api(`/api/${feature}/${encodeURIComponent(hhId)}`, { body }), (r) => { S.results[key] = r; renderResult(box, r, go); });
  };
  go(false);
  return { key, go };
}

function waterBasis(o, facts) {
  // P5b A: water_fit is established only by the code water budget; otherwise it is a rubric judgement.
  const key = { "Chana only": "gram", "Chana + safflower": "gram-safflower", "Jowar + fodder": "sorghum" }[o.name];
  const f = (facts || []).find((x) => x.fact_id === "F-wb-coverage-" + key);
  return f && f.available ? { ok: true, fact: f } : { ok: false, fact: f };
}

function optionCardF3(o, rec, scope, facts) {
  const isRec = o.name === rec;
  const wb = waterBasis(o, facts);
  return el("article", { class: "card p-4 " + (isRec ? "ring-4 ring-green-700" : ""), "aria-label": optName(o.name) },
    isRec ? el("div", { class: "text-xs font-bold text-primary mb-1" }, "★ " + T("f3.recommended")) : null,
    el("h3", { class: "font-bold text-lg" }, optName(o.name)),
    el("div", { class: "mt-2" }, signalBadge(o.signal, true)),
    el("div", { class: "mt-3" }, riskBar(o.failure_risk)),
    el("div", { class: "flex flex-wrap gap-2 mt-3" },
      chip(`💧 ${T("f3.water_fit")}: ${T("water_fit." + o.water_fit, o.water_fit)}` + (wb.ok ? ` (${wb.fact.range.join("–")}%)` : ` · ${T("f3.not_established")}`),
        o.water_fit === "short" ? "#FFEBEE" : o.water_fit === "tight" ? "#FFF8E1" : "#E8F5E9", "#263238"),
      chip(`₹ ${T("f3.cash_at_risk")}: ${T("level." + o.cash_at_risk, o.cash_at_risk)}`, "#ECEFF1", "#263238"),
      confChip(o.confidence)),
    el("h4", { class: "font-semibold mt-3 text-sm" }, T("f3.drivers")),
    el("ul", { class: "flex flex-col gap-1 text-sm" }, (o.drivers || []).map((d) => driverLine(d, scope))));
}

function viewF3(main, hhId, q, fresh) {
  const ctl = runFeature(main, "f3", hhId, q, fresh, (box, r, go) => renderF3(box, r, go, hhId));
  if (q.get("learn") === "1") setTimeout(() => { const b = document.getElementById("learning-demo-btn"); if (b) { b.focus(); b.scrollIntoView({ block: "center" }); } }, 300);
  return ctl;
}

function renderF3(box, r, go, hhId) {
  const scope = "res-" + r.request_id;
  add(box, el("div", { id: scope },
    resultBanners(r),
    el("div", { class: "flex items-center gap-2 mt-3 flex-wrap" }, chip(`✅ ${T("trace.verifier")}: ${r.verifier_status}`, r.verifier_status === "PASS" ? "#E8F5E9" : "#FFEBEE", "#263238"),
      r.context_bucket ? chip(r.context_bucket, "#ECEFF1", "#263238") : null),
    el("div", { class: "grid gap-3 md:grid-cols-3 mt-3" }, (r.options || []).map((o) => optionCardF3(o, r.withheld ? null : r.recommended, scope, r.facts))),
    traceTimeline(r, scope), explainPanel(r, scope),
    feedbackBar(r, "f3", hhId, () => go(true), () => learningDemo(box, "f3", hhId, r))));
}

// ---------- 5. F4 livestock ----------

function moneyLine(label, m, scope) {
  if (!m) return null;
  return el("div", { class: "text-sm" }, el("div", { class: "text-gray-700" }, label),
    el("div", { class: "text-xl font-bold" }, fmtRs(m.value), factChip(m.fact_id, scope)),
    m.low !== null && m.low !== undefined ? el("div", { class: "text-xs text-gray-700" }, `${T("f4.range")}: ${fmtRs(m.low)} – ${fmtRs(m.high)}`) : null);
}

function supportList(ids, facts) {
  if (!ids || !ids.length || !S.support) return null;
  const camp = (facts || []).find((f) => f.fact_id === "F-f4-camp");
  return el("div", { class: "mt-3" }, el("h4", { class: "font-semibold text-sm" }, "🤝 " + T("f4.support")),
    el("ul", { class: "text-sm" }, ids.map((id) => {
      const s = S.support[id];
      if (!s) return el("li", {}, id + ": " + T("common.not_available"));
      return el("li", { class: "py-1" }, el("span", { class: "font-semibold" }, s["name_" + S.lang] || s.name_en),
        id === "fodder_camp" && camp && camp.caveat ? el("span", { class: "ml-1" }, chip("⚠ " + camp.caveat, "#FFF3C4", "#5D4037")) : null,
        el("div", { class: "text-xs text-gray-700" }, s.what), el("div", { class: "text-xs" }, `${T("f4.confirm_with")}: ${s.confirm_with} · `, link(s.source_url, T("common.source"))));
    })));
}

function optionCardF4(o, rec, scope, facts) {
  const isRec = o.name === rec;
  return el("article", { class: "card p-4 " + (isRec ? "ring-4 ring-green-700" : "") },
    isRec ? el("div", { class: "text-xs font-bold text-primary mb-1" }, "★ " + T("f3.recommended")) : null,
    el("h3", { class: "font-bold text-lg" }, optName(o.name)),
    el("div", { class: "grid grid-cols-2 gap-3 mt-3" }, moneyLine("₹ " + T("f4.cash_needed"), o.cash_needed, scope),
      moneyLine("🐄 " + T("f4.asset_kept"), o.asset_value_kept, scope), o.sale_proceeds ? moneyLine("💰 " + T("f4.sale_proceeds"), o.sale_proceeds, scope) : null),
    el("div", { class: "flex flex-wrap gap-2 mt-3" }, levelChip(o.cash_pressure && o.cash_pressure.toUpperCase(), T("f4.cash_pressure")), confChip(o.confidence)),
    el("div", { class: "mt-3" }, riskBar(o.risk)),
    el("h4", { class: "font-semibold mt-3 text-sm" }, T("f3.drivers")),
    el("ul", { class: "flex flex-col gap-1 text-sm" }, (o.drivers || []).map((d) => driverLine(d, scope))),
    supportList(o.support_ids, facts));
}

function viewF4(main, hhId, q, fresh) {
  return runFeature(main, "f4", hhId, q, fresh, (box, r, go) => renderF4(box, r, go, hhId));
}

function renderF4(box, r, go, hhId) {
  const scope = "res-" + r.request_id;
  if (r.applicable === false) {
    add(box, el("div", { id: scope }, el("div", { class: "card p-4 mt-3 border-l-8", style: { borderLeftColor: "#6D4C41" } },
      el("div", { class: "text-2xl font-bold" }, "🐄 " + T("f4.na_title")), el("p", { class: "mt-1" }, T("f4.na_body")),
      r.reason ? el("p", { class: "text-sm text-gray-700 mt-1" }, r.reason) : null), traceTimeline(r, scope)));
    return;
  }
  const goat = (r.facts || []).find((f) => f.fact_id === "F-f4-goats-note");
  add(box, el("div", { id: scope },
    resultBanners(r),
    goat ? el("div", { class: "mt-2" }, chip("🐐 " + T("f4.goat_note"), "#FFF3C4", "#5D4037"), el("span", { class: "text-xs ml-2" }, goat.text), factChip(goat.fact_id, scope)) : null,
    el("div", { class: "flex items-center gap-2 mt-3 flex-wrap" }, chip(`✅ ${T("trace.verifier")}: ${r.verifier_status}`, r.verifier_status === "PASS" ? "#E8F5E9" : "#FFEBEE", "#263238"),
      r.context_bucket ? chip(r.context_bucket, "#ECEFF1", "#263238") : null),
    el("div", { class: "grid gap-3 md:grid-cols-3 mt-3" }, (r.options || []).map((o) => optionCardF4(o, r.withheld ? null : r.recommended, scope, r.facts))),
    traceTimeline(r, scope), explainPanel(r, scope),
    feedbackBar(r, "f4", hhId, () => go(true), () => learningDemo(box, "f4", hhId, r))));
}

// ---------- learning demo (F3/F4): 3 loss outcomes -> re-run -> before/after ----------

async function learningDemo(box, feature, hhId, base) {
  // Each click = one round: 3 loss outcomes for the SAME graded plan (base), then a re-run. Before = the previous
  // round's After, so rounds chain. "What changed" only compares the two API results; nothing is computed.
  const key = `${feature}:${hhId}:${base.request_id}`;
  S.learn = S.learn || {};
  const L = S.learn[key] = S.learn[key] || { last: base, round: 0 };
  const round = ++L.round;
  const before = L.last;
  const rec = base.recommended;
  const panel = el("div", { class: "card p-4 mt-4 border-l-8", style: { borderLeftColor: "#6D4C41" } },
    el("h2", { class: "font-bold text-lg text-primary" }, `🎓 ${T("feedback.learning_demo")} · ${T("feedback.round")} ${round}`),
    el("p", { class: "text-sm" }, `+3 ${T("feedback.loss")} → ${optName(rec)} (${T("confidence." + ((base.options || []).find((o) => o.name === rec) || {}).confidence, "")})`));
  box.prepend(panel);
  panel.scrollIntoView({ behavior: "smooth" });
  const l = loader();
  add(panel, l.node);
  try {
    for (let i = 1; i <= 3; i++) {
      await api("/api/outcomes", { body: { hh_id: hhId, request_id: base.request_id, season: `learning-demo-r${round}-${i}`, actual_result: "loss", note: "learning demo", demo: true } });
    }
    const after = await api(`/api/${feature}/${encodeURIComponent(hhId)}`, { body: {} });
    L.last = after;
    l.stop();
    l.node.remove();
    const byName = (r) => Object.fromEntries((r.options || []).map((o) => [o.name, o]));
    const b = byName(before), a = byName(after);
    const changes = [];
    if (before.recommended !== after.recommended) changes.push(["★ " + T("f3.recommended"), optName(before.recommended), optName(after.recommended)]);
    for (const name of Object.keys(a)) {
      const x = b[name] || {}, y = a[name];
      if (x.confidence !== y.confidence) changes.push([optName(name) + " · " + T("f3.confidence"), T("confidence." + x.confidence, x.confidence || "–"), T("confidence." + y.confidence, y.confidence)]);
      if (y.signal && x.signal !== y.signal) changes.push([optName(name), T("signal." + x.signal, x.signal || "–"), T("signal." + y.signal, y.signal)]);
    }
    const ev = (r) => (r.explain && r.explain.evidence && r.explain.evidence.text) || T("common.not_available");
    const calNotes = (after.notes || []).filter((n) => /calibration/i.test(n) && n.includes(rec));
    const col = (label, r) => el("div", { class: "rounded-xl p-3", style: { background: "#FAFAF5" } },
      el("div", { class: "font-bold" }, label),
      el("ul", { class: "text-sm mt-1" }, (r.options || []).map((o) => el("li", { class: "py-1 flex flex-wrap gap-1 items-center" },
        el("span", { class: "font-semibold" }, (o.name === r.recommended ? "★ " : "") + optName(o.name) + ":"), o.signal ? signalBadge(o.signal, false) : null, confChip(o.confidence)))),
      el("div", { class: "text-xs mt-2" }, "📚 " + ev(r)));
    add(panel,
      el("div", { class: "mt-2 rounded-xl p-3", style: { background: changes.length ? "#E8F5E9" : "#FFF8E1" } },
        el("div", { class: "font-bold" }, changes.length ? "🔁 " + T("feedback.changed") : "⏳ " + T("feedback.no_change")),
        changes.length ? el("ul", { class: "text-sm mt-1" }, changes.map(([what, x, y]) => el("li", { class: "py-0.5" },
          el("span", { class: "font-semibold" }, what + ": "), el("span", { class: "line-through text-gray-700" }, x), " → ", el("span", { class: "font-bold" }, y)))) : null,
        calNotes.length ? el("ul", { class: "text-xs mt-2 list-disc ml-4" }, calNotes.map((n) => el("li", {}, n))) : null,
        el("div", { class: "text-xs mt-2" }, `📚 ${ev(before)} → ${ev(after)}`)),
      el("details", { class: "mt-2" }, el("summary", { class: "tap flex items-center text-accent text-sm" }, `${T("feedback.before")} / ${T("feedback.after")}`),
        el("div", { class: "grid gap-3 sm:grid-cols-2 mt-2" }, col(T("feedback.before"), before), col(T("feedback.after"), after))),
      el("div", { class: "text-xs text-gray-700 mt-2" }, `request ${before.request_id} → ${after.request_id}`));
  } catch (err) {
    L.round--;
    l.stop();
    l.node.replaceWith(errorCard(err, () => { panel.remove(); learningDemo(box, feature, hhId, base); }));
  }
}

// ---------- 6. F1 interview ----------

const F1 = { history: [], last: null, saved: null };

function viewF1(main) {
  put(main, pageHeader(T("f1.title"), null, button("↺ " + T("f1.restart"), () => { F1.history = []; F1.last = null; F1.saved = null; viewF1(main); }, "ghost")));
  const chat = el("div", { class: "card p-4 mt-3 flex flex-col gap-2", "aria-live": "polite" });
  const status = el("div", { class: "mt-2" });
  const side = el("div");
  const input = el("textarea", { rows: "3", class: "border rounded-xl p-3 w-full", placeholder: T("f1.placeholder"), "aria-label": T("f1.intro") });
  const renderChat = () => {
    put(chat, el("p", { class: "text-sm text-gray-700" }, T("f1.intro")),
      F1.history.map((t) => el("div", { class: "max-w-[85%] rounded-2xl px-3 py-2 " + (t.role === "agent" ? "self-start bg-green-50 border border-green-200" : "self-end bg-sky-50 border border-sky-200") },
        t.role === "agent" ? el("div", { class: "text-xs text-primary font-semibold" }, "❓ " + T("f1.questions")) : null, t.text)));
  };
  const handle = (r) => {
    F1.last = r;
    if (r.transcript) put(status, el("div", { class: "text-sm" }, `🎙️ ${T("f1.transcript")}: “${r.transcript}”`));
    if (r.type === "question" && r.question) F1.history.push({ role: "agent", text: r.question, field: r.field_targeted || null });
    renderChat();
    renderSide();
  };
  const send = async () => {
    const text = input.value.trim();
    if (!text) return;
    F1.history.push({ role: "user", text });
    input.value = "";
    renderChat();
    const l = loader();
    put(status, l.node);
    try { const r = await api("/api/f1/interview", { body: { history: F1.history, lang: S.lang } }); l.stop(); put(status); handle(r); }
    catch (err) { l.stop(); F1.history.pop(); renderChat(); input.value = text; put(status, errorCard(err, send)); }
  };
  const mic = has("/api/f1/audio") && window.MediaRecorder && navigator.mediaDevices ? micButton(status, (r) => {
    if (r.transcript) F1.history.push({ role: "user", text: r.transcript });
    handle(r);
  }) : null;
  const renderSide = () => {
    const r = F1.last;
    if (!r) { put(side); return; }
    const profile = r.type === "final" ? r.profile : r.profile_so_far;
    const weights = S.cfg && S.cfg.profile ? S.cfg.profile.completeness_weights : null;
    const vc = r.trace && r.trace.find((s) => s.step === "verifier_code");
    const derived = (profile && profile.derived && profile.derived.length ? profile.derived : vc && vc.detail.derived) || [];
    const cstep = r.trace && r.trace.find((s) => s.step === "completeness");
    const flags = (profile && profile.flags) || (cstep && cstep.detail.flags) || [];
    const scope = "res-" + r.request_id;
    put(side, el("div", { id: scope },
      section("📊 " + T("f1.completeness"),
        el("div", { class: "flex justify-between font-semibold" }, el("span", {}, r.completeness_pct + "%"), el("span", { class: "text-sm" }, `${T("f1.questions")}: ${r.questions_asked}/3`)),
        el("div", { class: "h-3 bg-gray-200 rounded-full mt-1" }, el("div", { class: "h-3 rounded-full", style: { width: r.completeness_pct + "%", background: flags.includes("incomplete") ? "#F9A825" : "#1B5E20" } })),
        weights ? el("div", { class: "text-xs text-gray-700 mt-1" }, T("f1.weights") + ": " + Object.entries(weights).map(([k, v]) => `${k} ${v}`).join(" · ")) : null,
        flags.length ? el("div", { class: "mt-2" }, banner("📋 " + T("banner.incomplete"), "warn")) : null),
      section("👪 " + (r.type === "final" ? T("f1.final") : T("f1.so_far")),
        profile ? el("div", { class: "flex flex-wrap gap-1" }, Object.entries(profile).filter(([k, v]) => !["flags", "derived"].includes(k) && !isUnknown(v))
          .map(([k, v]) => chip(`${k}: ${v}`, "#E8F5E9", "#1B5E20"))) : T("common.not_available"),
        derived.length ? el("div", { class: "text-sm mt-2" }, `${T("f1.derived")}: `, derived.map((x) => (typeof x === "string" ? x : `${x.field} = ${x.value}${x.reason ? " (" + x.reason + ")" : ""}`)).join("; ")) : null,
        r.unknown_fields && r.unknown_fields.length ? el("div", { class: "text-sm mt-2" }, `${T("f1.unknown")}: `, r.unknown_fields.join(", ")) : null),
      r.type === "final" ? saveCard(profile) : null,
      traceTimeline(r, scope)));
  };
  add(main, chat, el("div", { class: "card p-3 mt-3" }, input,
    el("div", { class: "flex flex-wrap gap-2 mt-2" }, button("➤ " + T("common.send"), send, "primary"), mic)), status, side);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send(); });
  renderChat();
  renderSide();
}

function micButton(status, onResult) {
  let rec = null, chunks = [];
  const b = button("🎙️ " + T("f1.mic"), async () => {
    if (rec && rec.state === "recording") { rec.stop(); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = ["audio/webm", "audio/ogg"].find((m) => MediaRecorder.isTypeSupported(m)) || "";
      rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      chunks = [];
      rec.ondataavailable = (e) => chunks.push(e.data);
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        b.textContent = "🎙️ " + T("f1.mic");
        const type = (rec.mimeType || "audio/webm").split(";")[0];
        const blob = new Blob(chunks, { type });
        const form = new FormData();
        form.append("file", blob, "voice." + (type.includes("ogg") ? "ogg" : "webm"));
        form.append("mime_type", type);
        form.append("history", JSON.stringify(F1.history));
        form.append("lang", S.lang);
        const l = loader();
        put(status, l.node);
        try { const r = await api("/api/f1/audio", { form }); l.stop(); put(status); onResult(r); }
        catch (err) { l.stop(); put(status, el("div", { class: "card p-3 mt-2", role: "alert" }, "⚠ " + T("f1.mic_fail"), el("div", { class: "text-xs mt-1" }, err.message))); }
      };
      rec.start();
      b.textContent = "⏹ " + T("f1.mic_stop");
    } catch (err) {
      put(status, el("div", { class: "card p-3 mt-2", role: "alert" }, "⚠ " + T("f1.mic_fail"), el("div", { class: "text-xs mt-1" }, err.message)));
    }
  }, "accent", { "aria-label": T("f1.mic") });
  return b;
}

function saveCard(profile) {
  const village = el("select", { class: "border rounded-xl px-3 py-2 tap w-full", "aria-label": T("f1.village") },
    el("option", { value: "" }, T("f1.pick_village")),
    (S.villages || []).map((v) => el("option", { value: v.village_id }, `${v.name} (${v.taluka}, ${v.district})`)));
  const consent = el("input", { type: "checkbox", id: "consent", class: "w-6 h-6" });
  const status = el("div", { class: "text-sm mt-2", "aria-live": "polite" });
  const save = async () => {
    if (!consent.checked || !village.value) { status.textContent = "⚠ " + T("f1.consent_needed"); return; }
    try {
      const res = await api("/api/households", { body: { profile, village_id: Number(village.value), consent: true } });
      F1.saved = res;
      status.textContent = `✓ ${T("f1.saved")} ${res.hh_id}`;
      F1.history = []; F1.last = null;
      location.hash = "#/f3/" + encodeURIComponent(res.hh_id);
    } catch (err) { status.textContent = "⚠ " + err.message; }
  };
  return section("💾 " + T("common.save"), village,
    el("label", { class: "flex items-center gap-2 tap mt-2", for: "consent" }, consent, T("f1.consent")),
    el("div", { class: "mt-2" }, button(T("f1.save_open_f3"), save, "primary")), status);
}

// ---------- 7. F2 season watch ----------

function alertList(alerts) {
  if (!alerts.length) return el("p", { class: "text-sm" }, T("f2.no_alerts"));
  return el("ul", { class: "flex flex-col gap-2" }, alerts.map((a) => el("li", { class: "border rounded-xl p-3" },
    el("div", { class: "flex flex-wrap gap-2 items-center" }, levelChip(a.stress_level, T("f2.stress")),
      a.next_step ? chip(T("next_step." + a.next_step, a.next_step), "#E3F2FD", "#01579B") : null,
      chip(`${T("f2.via")} ${a.via}`, a.via === "pubsub" ? "#1B5E20" : "#ECEFF1", a.via === "pubsub" ? "#fff" : "#263238"),
      el("span", { class: "text-xs text-gray-700" }, `${a.hh_id} · ${a.plot_id} · ${a.created_at}`)),
    el("p", { class: "text-sm mt-1 whitespace-pre-line" }, S.lang === "hi" ? a.text_hi : a.text_mr),
    el("div", { class: "text-xs mt-1" }, (a.fact_ids || []).map((f) => el("span", { class: "fact-chip mr-1" }, f)),
      a.data_as_of ? ` · ${T("f2.data_up_to")} ${a.data_as_of}` : ""))));
}

function viewWatch(main) {
  put(main, pageHeader(T("f2.title")));
  const scanBox = el("div");
  const plotsBox = el("div");
  const alertsBox = el("div");
  const runScan = () => withLoading(scanBox, () => api("/api/f2/scan", { body: {} }), (r) => {
    const s = r.summary;
    add(scanBox, section("📋 " + T("f2.scan_summary"),
      el("div", { class: "flex flex-wrap gap-2" }, levelChip("HIGH", String(s.HIGH)), levelChip("MED", String(s.MED)), levelChip("LOW", String(s.LOW)),
        chip(`plots: ${s.plots}`, "#ECEFF1", "#263238"), chip(`alerts: ${s.alerts_created}`, "#ECEFF1", "#263238"), chip(`deduped: ${s.deduped}`, "#ECEFF1", "#263238")),
      s.skipped.length ? el("details", { class: "mt-2 text-sm" }, el("summary", { class: "tap flex items-center" }, `${T("f2.skipped")}: ${s.skipped.length}`),
        el("ul", { class: "list-disc ml-5" }, s.skipped.map((x) => el("li", {}, `${x.hh_id}: ${x.reason}`)))) : null));
    loadAlerts();
  });
  const loadAlerts = () => withLoading(alertsBox, () => api("/api/alerts"), (a) => add(alertsBox, section("🔔 " + T("f2.alerts"), alertList(a))));
  add(main, el("div", { class: "card p-4 mt-3" }, button("🛰️ " + T("f2.scan"), runScan, "primary"), el("div", { class: "text-xs text-gray-700 mt-1" }, T("f2.scan_hint"))),
    scanBox, plotsBox, alertsBox);
  withLoading(plotsBox, () => api("/api/households"), (rows) => {
    const plots = rows.flatMap((h) => h.plot_ids.map((p) => ({ p, h })));
    add(plotsBox, section("🗺️ " + T("f2.plots"), el("ul", { class: "grid gap-2 sm:grid-cols-2" }, plots.map(({ p, h }) => el("li", {},
      el("a", { href: "#/watch/" + encodeURIComponent(p), class: "tap border rounded-xl p-3 flex items-center justify-between gap-2 hover:border-primary" },
        el("span", {}, el("span", { class: "font-semibold block" }, p), el("span", { class: "text-xs text-gray-700" }, h.village || "")),
        h.latest_alert ? levelChip(h.latest_alert.stress_level) : null))))));
  });
  loadAlerts();
}

function lineChart(canvas, labels, datasets, yTitle) {
  if (!window.Chart) return;
  S.charts.push(new Chart(canvas, {
    type: "line", data: { labels, datasets },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { position: "bottom" } }, scales: { x: { ticks: { maxTicksLimit: 8 } }, y: { title: { display: true, text: yTitle } } } },
  }));
}

function viewPlot(main, plotId) {
  put(main, pageHeader(`${T("f2.title")} · ${plotId}`, "#/watch"));
  const charts = el("div");
  const runBox = el("div");
  add(main, charts, el("div", { class: "card p-4 mt-4" }, button("🛰️ " + T("f2.run"), () => runPlot(), "primary")), runBox);
  withLoading(charts, () => api(`/api/plots/${encodeURIComponent(plotId)}/series`), (s) => {
    const rainC = el("canvas", { "aria-label": T("f2.rain_chart"), role: "img" });
    const ndviC = el("canvas", { "aria-label": T("f2.ndvi_chart"), role: "img" });
    const rainSrc = [...new Set(s.rain.map((r) => r.source))].join(", ");
    const ndviSrc = [...new Set(s.ndvi.map((r) => r.source))].join(", ");
    const synthSrc = (src) => /SYNTHETIC/i.test(src);
    add(charts, 
      el("div", { class: "flex gap-2 flex-wrap mt-2" }, el("span", { class: "text-sm" }, `👪 ${s.hh_id}`), s.plot_is_synthetic ? chip("📍 plot location " + T("common.synthetic"), "#FFF3C4", "#5D4037") : null),
      section("🌧️ " + T("f2.rain_chart"), el("div", { class: "text-xs text-gray-700" }, `${T("common.source")}: ${rainSrc || T("common.not_available")}`, synthSrc(rainSrc) ? synthChip() : null),
        s.rain.length ? el("div", { style: { height: "240px" } }, rainC) : el("p", {}, T("common.not_available"))),
      section("🌿 " + T("f2.ndvi_chart"), el("div", { class: "text-xs text-gray-700" }, `${T("common.source")}: ${ndviSrc || T("common.not_available")}`, synthSrc(ndviSrc) ? synthChip() : null),
        s.ndvi.length ? el("div", { style: { height: "240px" } }, ndviC) : el("p", {}, T("common.not_available"))));
    const draw = () => {
      if (!window.Chart) { setTimeout(draw, 200); return; }
      if (s.rain.length) lineChart(rainC, s.rain.map((r) => r.date), [
        { label: T("f2.rain"), data: s.rain.map((r) => r.rain_mm), borderColor: "#0277BD", backgroundColor: "rgba(2,119,189,.25)", fill: true, pointRadius: 0, tension: 0.2 },
        { label: T("f2.normal"), data: s.rain.map((r) => r.normal_mm), borderColor: "#6D4C41", borderDash: [6, 4], pointRadius: 0, tension: 0.2 }], "mm");
      if (s.ndvi.length) lineChart(ndviC, s.ndvi.map((r) => r.date), [
        { label: T("f2.ndvi"), data: s.ndvi.map((r) => r.ndvi), borderColor: "#1B5E20", pointRadius: 3 },
        { label: T("f2.normal"), data: s.ndvi.map((r) => r.ndvi_normal), borderColor: "#6D4C41", borderDash: [6, 4], pointRadius: 2 }], "NDVI");
    };
    draw();
  });
  const runPlot = () => withLoading(runBox, () => api(`/api/f2/${encodeURIComponent(plotId)}`, { body: {} }), (r) => {
    const scope = "res-" + r.request_id;
    const asOf = (r.facts || []).find((f) => f.fact_id === "F-stress-asof");
    const a = r.alert;
    add(runBox, el("div", { id: scope },
      r.skipped ? banner(r.skipped, "info") : null,
      el("div", { class: "flex flex-wrap gap-2 mt-3 items-center" }, levelChip(r.stress_level, T("f2.stress")),
        r.next_step ? chip("➡ " + T("next_step." + r.next_step, r.next_step), "#E3F2FD", "#01579B") : null,
        asOf ? chip(`${T("f2.data_up_to")} ${asOf.value}`, "#ECEFF1", "#263238") : null),
      r.crossed && r.crossed.length ? el("div", { class: "text-sm mt-2" }, `${T("f2.crossed")}: ${r.crossed.join("; ")}`) : null,
      r.deduped ? el("div", { class: "mt-2" }, banner("🔁 " + T("f2.deduped"), "info")) : null,
      a ? section("🔔 " + T("f2.alerts"),
        el("div", {}, el("div", { class: "text-xs text-gray-700" }, T("f2.alert_mr")), el("p", { class: "whitespace-pre-line" }, a.text_mr), speakButton(a.text_mr, "mr")),
        el("div", { class: "mt-2" }, el("div", { class: "text-xs text-gray-700" }, T("f2.alert_hi")), el("p", { class: "whitespace-pre-line" }, a.text_hi), speakButton(a.text_hi, "hi")),
        el("div", { class: "mt-2" }, (a.fact_ids || []).map((f) => factChip(f, scope))),
        el("div", { class: "mt-2" }, chip(`${T("f2.via")} ${a.via}`, a.via === "pubsub" ? "#1B5E20" : "#ECEFF1", a.via === "pubsub" ? "#fff" : "#263238"))) : null,
      traceTimeline(r, scope)));
  });
}

// ---------- 8. learning ----------

function viewLearning(main) {
  put(main, pageHeader(T("learning.title")));
  const cal = el("div"), fb = el("div"), terms = el("div");
  add(main, cal, fb, terms);
  const c = (S.cfg && S.cfg.calibration) || {};
  withLoading(cal, () => api("/api/calibration"), (d) => {
    const flag = (r) => r.confidence_level === "HIGH" && r.hit_rate < c.high_min_hit_rate && r.n >= c.min_n;
    add(cal, section("📊 " + T("learning.calibration"), el("div", { class: "text-xs text-gray-700" }, `${T("learning.rule")}: ${d.rule}`),
      el("div", { class: "mt-1" }, banner("ℹ️ " + T("learning.heuristic_note"), "info")),
      el("div", { class: "scroll-x mt-2" }, el("table", { class: "w-full text-sm min-w-[520px]" },
        el("thead", {}, el("tr", { class: "text-left text-xs uppercase text-gray-700" }, ["context", "level", "hit_rate", "n", "n_real", "n_synthetic", "n_demo"].map((k) => el("th", { class: "p-2" }, T("learning." + k))))),
        el("tbody", {}, d.rows.map((r) => el("tr", { class: "border-t", style: flag(r) ? { background: "#FFEBEE" } : {} },
          el("td", { class: "p-2 font-mono text-xs" }, r.context_bucket), el("td", { class: "p-2" }, T("confidence." + r.confidence_level, r.confidence_level)),
          el("td", { class: "p-2 font-semibold" }, Math.round(r.hit_rate * 100) + "%" + (flag(r) ? " ⚠" : "")), el("td", { class: "p-2" }, r.n),
          el("td", { class: "p-2" }, r.n_real), el("td", { class: "p-2" }, r.n_synthetic, r.n_synthetic ? el("span", { class: "ml-1" }, synthChip()) : null),
          el("td", { class: "p-2" }, r.n_demo ?? T("common.not_available")))))))));
  });
  withLoading(fb, () => api("/api/feedback/review"), (rows) => add(fb, section("👎 " + T("learning.feedback"),
    rows.length ? el("ul", { class: "text-sm" }, rows.map((r) => el("li", { class: "border-t py-2" }, el("span", { class: "font-mono text-xs" }, r.request_id), ` · ${r.feature} · ${r.context_bucket || ""}`,
      el("div", {}, r.note || "—"), el("div", { class: "text-xs text-gray-700" }, String(r.created_at))))) : el("p", { class: "text-sm" }, T("learning.none")))));
  const loadTerms = () => withLoading(terms, () => api("/api/terms/review"), (d) => {
    const cats = (S.cfg && S.cfg.term_categories) || [];
    const pending = d.pending || [];
    add(terms, section("📖 " + T("learning.terms"),
      pending.length ? el("ul", { class: "flex flex-col gap-2" }, pending.map((p) => {
        const canon = el("input", { type: "text", class: "border rounded-lg px-2 py-1 tap", value: p.canonical || p.meaning || "", "aria-label": T("learning.canonical") });
        const cat = el("select", { class: "border rounded-lg px-2 py-1 tap", "aria-label": T("learning.category") }, cats.map((x) => el("option", { value: x }, x)));
        const st = el("span", { class: "text-xs", "aria-live": "polite" });
        const decide = async (status) => {
          try { await api("/api/terms/review", { body: { term: p.term, status, canonical: canon.value || null, category: cat.value || null } }); loadTerms(); }
          catch (err) { st.textContent = "⚠ " + err.message; }
        };
        return el("li", { class: "border rounded-xl p-3" }, el("div", { class: "font-semibold" }, p.term, el("span", { class: "text-sm text-gray-700 font-normal" }, p.meaning ? ` – ${p.meaning}` : "")),
          el("div", { class: "flex flex-wrap gap-2 mt-2 items-center" }, canon, cat, button("✔ " + T("learning.approve"), () => decide("approved"), "primary"), button("✖ " + T("learning.reject"), () => decide("rejected"), "ghost"), st));
      })) : el("p", { class: "text-sm" }, T("learning.none")),
      (d.approved || []).length ? el("div", { class: "text-sm mt-3" }, `${T("learning.approved")}: `, d.approved.map((x) => `${x.term} → ${x.canonical}`).join("; ")) : null,
      (d.rejected || []).length ? el("div", { class: "text-sm mt-1 text-gray-700" }, `${T("learning.rejected")}: `, d.rejected.map((x) => x.term).join(", ")) : null));
  });
  loadTerms();
}

// ---------- 9. impact (P5b): displays the stored run; every number comes from the API ----------

const STATUS_COLOR = { SIMULATED: ["#6D4C41", "#fff"], TESTED: ["#1B5E20", "#fff"], MEASURED: ["#0277BD", "#fff"],
  REPLAY: ["#01579B", "#fff"], TARGET: ["#ECEFF1", "#263238"], NOT_ESTABLISHED: ["#FFEBEE", "#B71C1C"], NOT_RUN: ["#ECEFF1", "#263238"] };

function kpiValue(k) {
  if (k.value === null || k.value === undefined) return el("div", { class: "text-2xl font-bold text-gray-700" }, T("impact.not_established"));
  const v = typeof k.value === "number" && Math.abs(k.value) >= 1000 ? k.value.toLocaleString("en-IN") : k.value;
  return el("div", {}, el("span", { class: "text-3xl font-bold text-primary" }, (k.id === "K1" ? "₹" : "") + v),
    el("span", { class: "text-sm text-gray-700 ml-2" }, k.unit));
}

function listBlock(title, items) {
  return items && items.length ? el("div", { class: "mt-2" }, el("div", { class: "font-semibold text-sm" }, title),
    el("ul", { class: "list-disc ml-5 text-sm" }, items.map((x) => el("li", {}, x)))) : null;
}

function kpiExtra(k) {
  if (k.id === "K1" && k.per_household_detail) {
    const rows = k.per_household_detail.filter((d) => d.eligible);
    const canvas = el("canvas", { role: "img", "aria-label": k.name });
    setTimeout(() => window.Chart && rows.length && S.charts.push(new Chart(canvas, { type: "bar",
      data: { labels: rows.map((d) => d.hh_id), datasets: [
        { label: T("impact.baseline"), data: rows.map((d) => d.baseline_mid), backgroundColor: "#BCAAA4" },
        { label: T("impact.recommended"), data: rows.map((d) => d.option_mid), backgroundColor: "#1B5E20" }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "bottom" } } } })), 50);
    return el("div", {}, rows.length ? el("div", { style: { height: "220px" }, class: "mt-2" }, canvas) : null,
      el("div", { class: "text-xs mt-1" }, `n_better ${k.n_better ?? "–"} / ${k.n_eligible} · median ₹${k.median ?? "–"} · ` +
        Object.entries(k.scenarios || {}).map(([s, v]) => `${s}: ₹${v.mean ?? "–"}`).join(" · ")),
      k.combined_feasibility ? el("div", { class: "text-xs text-soil" }, `combined F3+F4 feasibility: ${k.combined_feasibility.status} ` +
        `(missing: ${(k.combined_feasibility.missing_inputs || []).join("; ")})`) : null);
  }
  if (k.id === "K2" && k.n_eligible) {
    const canvas = el("canvas", { role: "img", "aria-label": k.name });
    setTimeout(() => window.Chart && S.charts.push(new Chart(canvas, { type: "bar",
      data: { labels: [T("impact.baseline"), T("impact.recommended")], datasets: [{ label: "% short",
        data: [k.baseline_short_pct, k.recommended_short_pct], backgroundColor: ["#C62828", "#1B5E20"] }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { min: 0, max: 100 } } } })), 50);
    return el("div", {}, el("div", { style: { height: "180px" }, class: "mt-2" }, canvas),
      el("div", { class: "text-xs" }, `paired ${k.n_eligible} · baseline short ${k.n_baseline_short} · recommended short ${k.n_recommended_short} · ` +
        `resolved ${k.resolved_short_pct ?? "–"}% · assumed baseline ${k.n_assumed_baseline}`));
  }
  if (k.id === "K3") {
    return el("div", { class: "text-xs mt-1" }, `${k.n_detected ?? 0}/${k.n_eligible ?? 0} plots reached HIGH · reference ${k.reference_date} ` +
      `(${k.reference_verified ? T("impact.verified") : T("impact.unverified")}) · data ${(k.coverage_period || []).join(" → ")}`);
  }
  if (k.id === "K4" && k.benchmark) {
    const b = k.benchmark;
    return el("div", {}, el("div", { class: "text-sm font-semibold mt-1" },
      `${b.errors_caught}/${b.errors_planned} ${T("impact.errors_caught")} · ${b.controls_accepted}/${b.controls_planned} ${T("impact.controls_accepted")}`),
      el("div", { class: "text-xs" }, Object.entries(b.by_type || {}).map(([t, v]) => `${t}: ${v.caught}/${v.planned}`).join(" · ")),
      el("details", { class: "mt-1" }, el("summary", { class: "tap flex items-center text-accent text-sm" }, T("impact.cases")),
        el("div", { class: "scroll-x" }, el("table", { class: "text-xs min-w-[520px] w-full" }, el("tbody", {}, b.cases.map((c) =>
          el("tr", { class: "border-t" }, el("td", { class: "p-1 font-mono" }, c.case_id), el("td", { class: "p-1" }, c.expected),
            el("td", { class: "p-1 font-semibold", style: { color: ["caught", "accepted"].includes(c.outcome) ? "#1B5E20" : "#C62828" } }, c.outcome),
            el("td", { class: "p-1" }, `code:${c.deterministic ? "flag" : "ok"} · agent:${c.semantic || "–"}`),
            el("td", { class: "p-1 break-anywhere" }, `${c.text} [${c.fact_id || "no id"}]`))))))));
  }
  if (k.id === "K5") {
    return el("div", { class: "text-xs mt-1" }, `gap before ${k.gap_before ?? "–"} → after ${k.gap_after ?? "–"} · downgraded ${k.n_downgraded ?? 0} · ` +
      `observed: ${k.observed_gap ?? k.observed_note}`, el("div", {}, k.correctness_definition || ""));
  }
  if (k.id === "K6" && k.coverage) {
    const c = k.coverage, r = k.runtime || {};
    return el("div", { class: "grid gap-2 sm:grid-cols-2 text-xs mt-1" },
      el("div", { class: "rounded-lg p-2", style: { background: "#FAFAF5" } }, el("div", { class: "font-semibold" }, T("impact.coverage")),
        `tenant/sharecrop ${c.tenant_or_sharecrop}/${c.n} · woman ${c.woman_primary_cultivator}/${c.n} · gender unknown ${c.gender_unknown}`),
      el("div", { class: "rounded-lg p-2", style: { background: "#FAFAF5" } }, el("div", { class: "font-semibold" }, T("impact.runtime")),
        `fresh decisions ${r.fresh_decisions} · p50 ${r.latency_p50_ms ?? "–"} ms · p90 ${r.latency_p90_ms ?? "–"} ms · model calls/decision ` +
        `${r.model_calls_mean ?? "–"} · F1 questions ${r.f1_questions_mean ?? "–"} · ${r.cost}`));
  }
  if (k.id === "K7") return el("div", { class: "text-xs mt-1" }, k.file_generation_note || "");
  return null;
}

function kpiCard(k) {
  const [bg, fg] = STATUS_COLOR[k.status] || ["#eee", "#333"];
  return el("article", { class: "card p-4", "aria-label": k.name },
    el("div", { class: "flex items-start justify-between gap-2" }, el("h3", { class: "font-bold" }, `${k.id} · ${k.name}`), chip(k.status, bg, fg)),
    el("div", { class: "mt-2" }, kpiValue(k)),
    el("p", { class: "text-sm mt-1" }, k.talking_point),
    k.n_total !== null && k.n_total !== undefined ? el("div", { class: "text-xs text-gray-700 mt-1" },
      `${T("impact.eligible")}: ${k.n_eligible ?? "–"}/${k.n_total}` + (k.n_excluded ? ` · ${T("impact.excluded")} ${k.n_excluded}` : "")) : null,
    kpiExtra(k),
    el("details", { class: "mt-2" }, el("summary", { class: "tap flex items-center text-accent text-sm" }, T("impact.how")),
      el("div", { class: "text-sm" }, el("div", {}, el("b", {}, "Formula: "), k.formula_text),
        k.baseline_definition ? el("div", {}, el("b", {}, "Baseline: "), k.baseline_definition) : null),
      listBlock(T("impact.excluded"), Object.entries(k.exclusion_reasons || {}).map(([r, n]) => `${n} × ${r}`)),
      listBlock(T("impact.sources"), k.data_sources), listBlock(T("impact.assumptions"), k.assumptions),
      listBlock(T("impact.limitations"), k.limitations)));
}

function viewImpact(main) {
  put(main, pageHeader(T("nav.impact")));
  if (!has("/api/impact/latest")) { add(main, el("div", { class: "card p-4 mt-3" }, T("nav.impact") + ": " + T("common.not_available"))); return; }
  const box = el("div");
  const status = el("div", { class: "text-xs text-gray-700", "aria-live": "polite" });
  const render = (d) => {
    put(status, `run ${d.run_id} · ${d.created_at} · ${d.meta.mode} · cache ${d.meta.cache_total - d.meta.cache_missing}/${d.meta.cache_total} · model calls ${d.meta.model_calls}`);
    put(box, d.honesty_banner ? el("div", { class: "mt-3" }, banner("ℹ️ " + d.honesty_banner, "info")) : null,
      el("div", { class: "grid gap-3 md:grid-cols-2 mt-3" }, d.kpis.map(kpiCard)));
  };
  const rerun = () => withLoading(box, async () => {
    try { return await api("/api/impact/run", { body: {} }); }
    catch (e) {   // 409: cache incomplete for this backend; show why and keep the last saved run
      if (e.status !== 409) throw e;
      const last = await api("/api/impact/latest");
      last.rerun_note = e.message.replace(/^409: /, "");
      return last;
    }
  }, (d) => { render(d); if (d.rerun_note) box.prepend(el("div", { class: "mt-3" }, banner("⚠ " + d.rerun_note, "warn"))); });
  add(main, el("div", { class: "flex flex-wrap items-center gap-2 mt-2" }, button("↻ " + T("impact.rerun"), rerun, "primary"), status), box);
  withLoading(box, () => api("/api/impact/latest").catch((e) => { if (e.status === 404) return null; throw e; }), (d) => {
    if (!d) { put(box, el("div", { class: "card p-4 mt-3" }, T("impact.no_run"))); return; }
    render(d);
  });
}

// ---------- 11. about ----------

function viewAbout(main) {
  const tech = [
    ["Gemini on Vertex AI", "All agent reasoning: F1 interview, F3 crop plan, F4 livestock, F2 alert text"],
    ["ADK", "Multi-agent orchestration: SequentialAgent of planner → assessor → verifier with function tools (plain engine = fallback)"],
    ["Vertex AI embeddings", "Household profile embeddings for similar-household search (F1/F3)"],
    ["BigQuery", "All tables on GCP; calibration view"],
    ["Earth Engine", "CHIRPS rainfall, Sentinel-2 NDVI, SMAP soil moisture (precomputed export)"],
    ["Cloud Storage", "Audio clips (and photos / evidence packs)"],
    ["Cloud Run", "One FastAPI service: UI + API + ADK engine"],
    ["Pub/Sub", "F2 publishes stress-alerts; push subscription to /api/alerts/receive stores and shows the alert"],
  ];
  const real = ["CHIRPS daily rainfall (Earth Engine)", "Sentinel-2 NDVI (Earth Engine)", "Agmarknet mandi prices (Dharashiv)", "Official MSP (PIB)", "Sourced fodder and cattle price reports (news, 2019 camp rate)", "Village coordinates (hand-entered)"];
  const synth = ["Households (except saved survey / F1 profiles)", "Plot locations near villages", "Past outcomes for calibration", "Normal animal values", "Fodder/cattle rows with basis = assumption"];
  put(main, pageHeader(T("about.title")),
    section("☁️ Google Cloud", el("ul", { class: "flex flex-col gap-2" }, tech.map(([k, v]) => el("li", { class: "text-sm" }, el("span", { class: "font-semibold" }, k + ": "), v)))),
    section("🧭 Architecture", el("p", { class: "text-sm" }, "UI → Cloud Run (FastAPI) → ADK planner / assessor / verifier → tools (facts only, each with fact_id + source) → BigQuery · Earth Engine exports · Pub/Sub · Cloud Storage. Code verifier + safety floor + calibration rule run after Gemini on every result.")),
    section("🔍 Real vs synthetic data", el("div", { class: "grid gap-3 sm:grid-cols-2" },
      el("div", {}, el("div", { class: "mb-1" }, realChip()), el("ul", { class: "list-disc ml-5 text-sm" }, real.map((x) => el("li", {}, x)))),
      el("div", {}, el("div", { class: "mb-1" }, synthChip()), el("ul", { class: "list-disc ml-5 text-sm" }, synth.map((x) => el("li", {}, x)))))),
    section("🛡️ Honesty", el("p", { class: "text-sm" }, "Every claim cites a fact_id; the verifier rejects claims without one. Mocked values carry a SYNTHETIC badge. No suicide or mental-health prediction: livelihood and financial distress language only.")));
}

// ---------- boot ----------

async function boot() {
  if (window.speechSynthesis) speechSynthesis.getVoices();   // starts Chrome's async voice loading

  const qs = new URLSearchParams(location.search);
  S.demo = qs.get("demo") === "1";
  S.lang = store("ss_lang") || "en";
  if (!LANGS.some(([c]) => c === S.lang)) S.lang = "en";
  const results = await Promise.allSettled([api(`i18n/${S.lang}.json`), api("/openapi.json"), api("/api/whoami"), api("/api/ui_config"), api("/api/villages"), api("/api/support_options")]);
  const val = (i) => (results[i].status === "fulfilled" ? results[i].value : null);
  S.t = val(0) || {};
  const spec = val(1);
  if (spec) { S.paths = new Set(Object.keys(spec.paths || {})); S.schemas = (spec.components && spec.components.schemas) || {}; }
  S.whoami = val(2); S.cfg = val(3); S.villages = val(4); S.support = val(5);
  window.addEventListener("hashchange", () => route(true));
  route(true);
}

boot();
