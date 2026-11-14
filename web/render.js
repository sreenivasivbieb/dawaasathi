// Pure renderers: QueryResponse JSON -> HTML string. No DOM access, so it runs under Node too.
// Every interpolated value goes through the `html` tagged template, which escapes it unless it
// is already an Html fragment. Interactive elements carry data-act="..." for event delegation.

import {
  t, sub, verdictTitle, flagLabel, useWord, formLabel, releaseLabel, packUnit, symptomChips,
} from "./i18n.js";

export class Html {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ESC[c]);
const flat = (v) =>
  v == null || v === false || v === true ? "" : v instanceof Html ? v.s : Array.isArray(v) ? v.map(flat).join("") : esc(v);
export function html(strings, ...vals) {
  let out = strings[0];
  for (let i = 0; i < vals.length; i++) out += flat(vals[i]) + strings[i + 1];
  return new Html(out);
}
const raw = (s) => new Html(s);

// ------------------------------------------------------------------ formatting helpers
const num = (n) => (n == null ? "" : Number.isInteger(+n) ? String(+n) : String(+(+n).toFixed(2)));
export const rupee = (n) =>
  n == null ? "" : "₹" + Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: Number.isInteger(+n) ? 0 : 2, maximumFractionDigits: 2 });
const titleCase = (s) => String(s || "").replace(/(^|[\s(/-])([a-z])/g, (m, a, b) => a + b.toUpperCase());

/** Mirrors SaltDose.label() in app/models.py. */
export function saltLabel(s) {
  const name = titleCase(s.name);
  if (s.amount == null) return name;
  const unit = s.unit || "";
  return (s.per_ml ? `${name} ${num(s.amount)} ${unit}/${num(s.per_ml)} ml` : `${name} ${num(s.amount)} ${unit}`).trim();
}
const saltsText = (item) =>
  (item?.salts || []).slice().sort((a, b) => a.name.localeCompare(b.name)).map(saltLabel).join(" + ");

// Latin digits in every script: prices and doses read the same as on the strip.
const LOCALE = (lang) => (lang === "en" ? "en-IN" : `${lang}-IN-u-nu-latn`);
export function fmtDate(iso, lang) {
  if (!iso) return "";
  const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(iso) ? iso + "T00:00:00" : iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  try {
    return d.toLocaleDateString(LOCALE(lang || "en"), { day: "numeric", month: "short", year: "numeric" });
  } catch { return String(iso); }
}

/** Bilingual label: primary in UI language, small English sub-label when the UI isn't English. */
export function L(lang, key, vars) {
  const s = sub(lang, key, vars);
  return html`<span class="lbl">${t(lang, key, vars)}</span>${s ? html`<span class="en">${s}</span>` : ""}`;
}

// ------------------------------------------------------------------ shapes (never colour alone)
const SHAPES = {
  check: '<svg viewBox="0 0 64 64" aria-hidden="true" focusable="false"><circle cx="32" cy="32" r="29" fill="currentColor"/><path d="M18 33l9.5 9.5L46 23" fill="none" stroke="var(--shape-ink)" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  cross: '<svg viewBox="0 0 64 64" aria-hidden="true" focusable="false"><rect x="4" y="4" width="56" height="56" rx="13" fill="currentColor"/><path d="M21 21l22 22M43 21L21 43" stroke="var(--shape-ink)" stroke-width="7" stroke-linecap="round"/></svg>',
  question: '<svg viewBox="0 0 64 64" aria-hidden="true" focusable="false"><path d="M28.4 7.6a4.2 4.2 0 0 1 7.2 0l25 43.6A4.2 4.2 0 0 1 57 57.5H7a4.2 4.2 0 0 1-3.6-6.3z" fill="currentColor"/><path d="M25.6 30.5a6.6 6.6 0 1 1 9.6 5.9c-2 1-3.2 2.6-3.2 4.8v1.3" fill="none" stroke="var(--shape-ink)" stroke-width="5.5" stroke-linecap="round"/><circle cx="32" cy="50" r="3.4" fill="var(--shape-ink)"/></svg>',
};
const GLYPH = {
  ok: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="11" fill="currentColor"/><path d="M6.5 12.5l3.5 3.5 7.5-8" fill="none" stroke="var(--shape-ink)" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  bad: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="1" y="1" width="22" height="22" rx="5" fill="currentColor"/><path d="M8 8l8 8M16 8l-8 8" stroke="var(--shape-ink)" stroke-width="2.6" stroke-linecap="round"/></svg>',
  warn: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10.4 2.9a1.8 1.8 0 0 1 3.2 0l9 16.4a1.8 1.8 0 0 1-1.6 2.7H3a1.8 1.8 0 0 1-1.6-2.7z" fill="currentColor"/><path d="M12 8.5v5.5" stroke="var(--shape-ink)" stroke-width="2.6" stroke-linecap="round"/><circle cx="12" cy="17.6" r="1.5" fill="var(--shape-ink)"/></svg>',
  info: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="11" fill="currentColor"/><path d="M12 11v6" stroke="var(--shape-ink)" stroke-width="2.6" stroke-linecap="round"/><circle cx="12" cy="7.2" r="1.5" fill="var(--shape-ink)"/></svg>',
};
const glyph = (tone) => raw(`<span class="glyph glyph--${tone}">${GLYPH[tone]}</span>`);
const ICON = {
  play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5v15l13-7.5z" fill="currentColor"/></svg>',
  stop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>',
  down: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11m-5-5 5 5 5-5M5 20h14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  plus: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>',
  shop: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9h16l-1.2-4.2A1.2 1.2 0 0 0 17.6 4H6.4a1.2 1.2 0 0 0-1.2.8zM5 9v10.5h14V9M9.5 19.5v-5h5v5" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>',
  camera: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8h3l1.6-2.4h6.8L17 8h3v11H4z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><circle cx="12" cy="13.2" r="3.4" fill="none" stroke="currentColor" stroke-width="2"/></svg>',
};
export const icon = (name) => raw(ICON[name] || "");

const WARN_FLAGS = new Set(["DUPLICATE_SALT", "SYMPTOM_MISMATCH", "LOOK_ALIKE", "NSQ_BATCH", "POSSIBLE_BANNED"]);
const SPECIAL_STATUS = ["needs_confirmation", "needs_image", "error", "blocked"];

/** Which title key a response maps to (verdict code, lookup variant, or status). */
export function titleKey(r) {
  if (SPECIAL_STATUS.includes(r.status)) return r.status;
  const code = r.verdict?.code;
  if (!code) return r.status === "unverified" ? "UNVERIFIED" : r.card?.title || "UNVERIFIED";
  if (r.verdict.mode === "lookup" && (code === "MATCH" || code === "GENERIC_EQUIVALENT")) return code + "_lookup";
  return code;
}

/** The item whose salts go to the home pouch / symptom check: dispensed (compare) or focus (lookup). */
export function targetItem(r) {
  const it = r.items || {};
  return it.dispensed || it.focus || null;
}
export function targetSalts(r) {
  return [...new Set((targetItem(r)?.salts || []).map((s) => s.name).filter(Boolean))];
}

// ------------------------------------------------------------------ sections
function head(r, lang) {
  const card = r.card || { color: "amber", icon: "question" };
  const key = titleKey(r);
  const title = verdictTitle(lang, key);
  const en = lang === "en" ? "" : verdictTitle("en", key);
  const shapeName = SHAPES[card.icon] ? card.icon : "question";
  return html`<header class="card__head">
    <span class="shape" role="img" aria-label="${t(lang, "shape_" + shapeName)}">${raw(SHAPES[shapeName])}</span>
    <div class="card__titles"><h2 class="card__title" id="verdict-title">${title}</h2>${en ? html`<p class="en">${en}</p>` : ""}</div>
  </header>`;
}

function said(r, lang) {
  if (!r.transcript) return "";
  return html`<section class="sec said"><h3>${L(lang, "you_said")}</h3><p class="said__text">“${r.transcript}”</p></section>`;
}

function matchSec(r, lang) {
  const m = r.match;
  if (!m || !m.brand || r.status === "needs_confirmation") return "";
  const pct = m.confidence != null ? Math.round(m.confidence * 100) : null;
  return html`<section class="sec match"><h3>${L(lang, "matched")}</h3>
    <p class="match__brand">${m.brand}</p>
    ${pct != null ? html`<div class="meter"><span class="meter__bar" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="${t(lang, "confidence")}"><span style="width:${pct}%"></span></span><span class="meter__txt">${t(lang, "confidence")} ${pct}%</span></div>` : ""}
    ${m.heard && m.source === "voice" ? html`<p class="small muted">${t(lang, "heard")}: “${m.heard}”</p>` : ""}
  </section>`;
}

function confirmSec(r, lang, o) {
  if (r.status !== "needs_confirmation") return "";
  const cands = r.candidates || [];
  return html`<section class="sec confirm"><h3>${L(lang, "confirm_q")}</h3>
    <div class="cands">
      ${cands.map((c, i) => html`<button class="cand" type="button" data-act="confirm" data-id="${c.candidate_id}">
        <span class="cand__n" aria-hidden="true">${i + 1}</span>
        <span class="cand__label">${c.label}</span>
        ${c.confidence != null && c.source !== "rx" ? html`<span class="cand__conf">${Math.round(c.confidence * 100)}%</span>` : ""}
      </button>`)}
      ${o.sessionId ? html`<button class="cand cand--none" type="button" data-act="confirm-none">${L(lang, "none_of_these")}</button>` : ""}
    </div>
    <p class="hint">${t(lang, "confirm_voice")}</p>
  </section>`;
}

function needsImageSec(r, lang) {
  if (r.status !== "needs_image") return "";
  return html`<section class="sec needimg"><p class="needimg__txt">${L(lang, "photo_hint")}</p>
    <div class="row-btns">
      <button class="btn btn--photo" type="button" data-act="pick" data-kind="strip">${icon("camera")}${L(lang, "strip_photo")}</button>
      <button class="btn btn--photo" type="button" data-act="pick" data-kind="rx">${icon("camera")}${L(lang, "rx_photo")}</button>
    </div></section>`;
}

function errorSec(r, lang) {
  if (r.status !== "error") return "";
  const code = r.data_sources?.error?.code || r.error?.code;
  const known = code && t(lang, "err_" + code) !== "err_" + code;
  return known ? html`<section class="sec err"><p>${t(lang, "err_" + code)}</p><p class="small muted">${code}</p></section>` : "";
}

function itemRows(lang, item) {
  const rows = [];
  if (item.brand) rows.push(["medicine", item.brand]);
  if (item.salts?.length) rows.push(["salts", saltsText(item)]);
  if (item.form) rows.push(["form", formLabel(lang, item.form)]);
  if (item.release) rows.push(["release", releaseLabel(lang, item.release)]);
  if (item.mrp != null) {
    const qty = item.pack_qty ? ` · ${num(item.pack_qty)} ${packUnit(lang, item.pack_unit, item.form)}` : "";
    rows.push(["mrp", rupee(item.mrp) + qty]);
  }
  if (item.manufacturer) rows.push(["maker", item.manufacturer]);
  return rows;
}

function itemsSec(r, lang) {
  const it = r.items || {};
  if (r.mode === "compare" && it.prescribed && it.dispensed) {
    const p = it.prescribed, d = it.dispensed;
    const salts = (x) => saltsText(x) || "—";
    const rows = [
      ["medicine", p.brand || "—", d.brand || "—", false],
      ["salts", salts(p), salts(d), salts(p) !== salts(d)],
      ["form", formLabel(lang, p.form) || "—", formLabel(lang, d.form) || "—", !!(p.form && d.form && p.form !== d.form)],
      ["release", releaseLabel(lang, p.release) || "—", releaseLabel(lang, d.release) || "—",
        !!(p.release && d.release && p.release !== d.release)],
      ["mrp", p.mrp != null ? rupee(p.mrp) : "—", d.mrp != null ? rupee(d.mrp) : "—", false],
    ];
    return html`<section class="sec items"><div class="cmp" role="table" aria-label="${t(lang, "prescribed")} / ${t(lang, "dispensed")}">
      <div class="cmp__row cmp__hdr" role="row"><span role="columnheader"></span>
        <span role="columnheader">${L(lang, "prescribed")}</span><span role="columnheader">${L(lang, "dispensed")}</span></div>
      ${rows.map(([k, a, b, diff]) => html`<div class="cmp__row${diff ? " is-diff" : ""}" role="row">
        <span role="rowheader" class="cmp__k">${t(lang, k)}${diff ? html`<span class="diff-tag">≠ ${t(lang, "differs")}</span>` : ""}</span>
        <span role="cell">${a}</span><span role="cell">${b}</span></div>`)}
    </div></section>`;
  }
  const item = it.focus || it.dispensed || it.prescribed;
  if (!item) return "";
  const rows = itemRows(lang, item);
  if (!rows.length) return "";
  return html`<section class="sec items"><dl class="kv">${rows.map(([k, v]) => html`<div><dt>${t(lang, k)}</dt><dd>${v}</dd></div>`)}</dl></section>`;
}

function safetySec(r, lang) {
  const s = r.safety;
  if (!s) return "";
  const map = {
    banned: ["bad", "banned_yes"], not_listed: ["ok", "banned_no"],
    unverified: ["warn", "banned_unverified"], possible: ["warn", "banned_possible"],
  };
  const [tone, key] = map[s.status] || ["warn", "banned_unverified"];
  const notif = [s.notification_ref, fmtDate(s.notification_date, lang)].filter(Boolean).join(" · ");
  return html`<section class="sec safety tone-${tone}"><h3>${L(lang, "safety")}</h3>
    <p class="statusline">${glyph(tone)}<span>${L(lang, key)}</span></p>
    <dl class="meta">
      ${notif ? html`<div><dt>${t(lang, "notification")}</dt><dd>${notif}</dd></div>` : ""}
      ${s.list_as_of ? html`<div><dt>${t(lang, "list_as_of")}</dt><dd>${fmtDate(s.list_as_of, lang)}</dd></div>` : ""}
      ${s.source ? html`<div><dt>${t(lang, "source")}</dt><dd>${s.source}</dd></div>` : ""}
    </dl></section>`;
}

function priceSec(r, lang) {
  const p = r.price;
  if (!p || (p.mrp == null && p.reference == null)) return "";
  const item = targetItem(r) || {};
  const unit = packUnit(lang, item.pack_unit, item.form);
  const forQty = (q) => (q ? t(lang, "for_qty", { qty: num(q), unit }) : "");
  return html`<section class="sec price"><h3>${L(lang, "price")}</h3>
    <div class="price__grid">
      ${p.mrp != null ? html`<div class="price__cell"><span class="price__k">${t(lang, "mrp")}</span><span class="price__v">${rupee(p.mrp)}</span><span class="price__q">${forQty(p.pack_qty)}</span></div>` : ""}
      ${p.reference != null ? html`<div class="price__cell price__cell--ref"><span class="price__k">${t(lang, "ref_price")}</span><span class="price__v">${rupee(p.reference)}</span><span class="price__q">${forQty(p.reference_pack_qty)}</span></div>` : ""}
    </div>
    ${p.ratio != null && p.reference != null ? html`<p class="price__ratio">${t(lang, "ratio", { x: num(p.ratio) })}</p>` : ""}
    ${p.above_ceiling === true ? html`<p class="statusline tone-warn">${glyph("warn")}<span>${L(lang, "above_ceiling")}</span></p>` : ""}
    ${p.stale ? html`<p class="small muted">${t(lang, "stale")}</p>` : ""}
    ${p.reference_name || p.source ? html`<p class="src">${[p.reference_name, p.source, fmtDate(p.as_of, lang)].filter(Boolean).join(" · ")}</p>` : ""}
  </section>`;
}

function altsSec(r, lang) {
  const alts = (r.alternatives || []).slice(0, 3);
  if (!alts.length) return "";
  const item = targetItem(r) || {};
  const unit = packUnit(lang, item.pack_unit, item.form);
  return html`<section class="sec alts"><h3>${L(lang, "alternatives")}</h3><ol class="alts__list">
    ${alts.map((a) => html`<li class="alt">
      <div class="alt__top"><span class="alt__name">${a.name}</span>${a.is_jan_aushadhi ? html`<span class="badge badge--ja">${t(lang, "ja_badge")}</span>` : ""}</div>
      <div class="alt__mid"><span class="alt__price">${rupee(a.price)}</span>${a.pack_qty ? html`<span class="alt__qty">${t(lang, "for_qty", { qty: num(a.pack_qty), unit })}</span>` : ""}
        ${a.saves_pct ? html`<span class="badge badge--save">${t(lang, "saves", { n: a.saves_pct })}</span>` : ""}</div>
      <div class="src">${[a.source, fmtDate(a.as_of, lang)].filter(Boolean).join(" · ")}</div>
    </li>`)}
  </ol></section>`;
}

function flagDetail(f, lang) {
  const x = f.facts || {};
  switch (f.code) {
    case "DUPLICATE_SALT": return (x.salts || []).map(titleCase).join(", ");
    case "SYMPTOM_MISMATCH":
    case "SYMPTOM_OK":
      return x.usual_use?.length ? `${flagLabel(lang, "usually")}: ${x.usual_use.map((u) => useWord(lang, u)).join(", ")}` : "";
    case "LOOK_ALIKE": return (x.similar_to || []).join(", ");
    case "NSQ_BATCH": return x.batch_no || "";
    case "POSSIBLE_BANNED": return x.notification_ref || "";
    case "STRENGTH_MISMATCH":
    case "CONCENTRATION_MISMATCH":
      return (x.salts || []).map((d) => `${d.prescribed} ≠ ${d.dispensed}`).join("; ");
    case "FORM_MISMATCH": return x.prescribed ? `${formLabel(lang, x.prescribed)} ≠ ${formLabel(lang, x.dispensed)}` : "";
    case "RELEASE_MISMATCH": return x.prescribed ? `${releaseLabel(lang, x.prescribed)} ≠ ${releaseLabel(lang, x.dispensed)}` : "";
    case "SALT_MISMATCH": return [...(x.prescribed || [])].map(titleCase).join(" + ") + " ≠ " + [...(x.dispensed || [])].map(titleCase).join(" + ");
    default: return "";
  }
}

function flagsSec(r, lang) {
  const primary = r.verdict?.code;
  const seen = new Set();
  const flags = (r.flags || []).filter((f) => f.code !== primary && !seen.has(f.code) && seen.add(f.code));
  if (!flags.length) return "";
  return html`<section class="sec flags"><h3>${L(lang, "flags")}</h3><ul class="flags__list">
    ${flags.map((f) => {
      const tone = f.code === "SYMPTOM_OK" ? "ok" : f.code === "NO_COMPARISON" ? "info"
        : WARN_FLAGS.has(f.code) || f.code.endsWith("_MISMATCH") ? "warn" : "info";
      const detail = flagDetail(f, lang);
      return html`<li class="flag tone-${tone}">${glyph(tone)}<span><span class="flag__lbl">${flagLabel(lang, f.code)}</span>
        ${lang !== "en" ? html`<span class="en">${flagLabel("en", f.code)}</span>` : ""}
        ${detail ? html`<span class="flag__detail">${detail}</span>` : ""}</span></li>`;
    })}
  </ul></section>`;
}

function audioRow(r, lang, compact = false) {
  const hasAudio = r.audio_stream_url || r.script?.text;
  if (!hasAudio) return "";
  return html`<div class="audio-row">
    <button class="btn btn--primary" type="button" data-act="replay">${icon("play")}${L(lang, "replay")}</button>
    <button class="btn" type="button" data-act="stop-audio" aria-label="${t(lang, "stop")}">${icon("stop")}<span class="lbl">${t(lang, "stop")}</span></button>
    ${!compact && r.audio_url && r.tts === "timbre" ? html`<a class="btn btn--ghost" href="${r.audio_url}" download="dawa-saathi-reply.ogg">${icon("down")}<span class="lbl">${t(lang, "download")}</span></a>` : ""}
  </div>
  ${!compact && r.tts !== "timbre" ? html`<p class="small muted">${t(lang, "mock_audio")}${r.audio_url ? html` · <a href="${r.audio_url}" download="dawa-saathi-reply.ogg">.ogg</a>` : ""}</p>` : ""}`;
}

function replySec(r, lang, compact = false) {
  const text = r.script?.text;
  if (!text && !r.audio_stream_url) return "";
  return html`<section class="sec reply"><h3>${L(lang, "reply")}</h3>
    ${text ? html`<p class="reply__text" lang="${r.lang || lang}">${text}</p>` : ""}
    ${audioRow(r, lang, compact)}</section>`;
}

function chemistSec(r, lang, o) {
  const ct = r.script?.chemist_text;
  if (!ct) return "";
  const open = !!o.chemistOpen;
  return html`<section class="sec chemist">
    <button class="btn btn--chemist" type="button" data-act="chemist-toggle" aria-expanded="${open ? "true" : "false"}" aria-controls="chemist-panel">${icon("shop")}${L(lang, "chemist_toggle")}</button>
    <div class="chemist__panel" id="chemist-panel"${open ? "" : raw(" hidden")}>
      <p class="small muted">${t(lang, "chemist_hint")}</p>
      <p class="chemist__text" lang="${r.lang || lang}">${ct}</p>
      <button class="btn btn--primary btn--wide" type="button" data-act="chemist-play">${icon("play")}${L(lang, "chemist_play")}</button>
    </div></section>`;
}

function pouchSec(r, lang, o) {
  const names = targetSalts(r);
  if (!names.length || r.status === "error") return "";
  const pouch = new Set(o.pouch || []);
  const all = names.every((n) => pouch.has(n));
  return html`<section class="sec pouchadd">
    <button class="btn btn--wide" type="button" data-act="pouch-add"${all ? raw(" disabled") : ""}>${all ? glyph("ok") : icon("plus")}${L(lang, all ? "added_pouch" : "add_pouch")}</button>
    <p class="small muted">${names.map(titleCase).join(" + ")}</p></section>`;
}

function symptomsSec(r, lang) {
  if (!(r.status === "ok" || r.status === "unverified") || !r.verdict || !targetSalts(r).length) return "";
  return html`<section class="sec symptoms"><h3>${L(lang, "symptom_q")}</h3><div class="chips">
    ${symptomChips(lang).map((c) => html`<button class="chip" type="button" data-act="symptom" data-text="${c.send}">${c.label}${c.en ? html`<small>${c.en}</small>` : ""}</button>`)}
  </div></section>`;
}

function foot(lang) {
  return html`<footer class="card__foot">${glyph("info")}<span>${L(lang, "doctor_footer")}</span></footer>`;
}

/**
 * Full verdict card for a QueryResponse.
 * o = { lang, phone: bool, pouch: string[], chemistOpen: bool, sessionId }
 */
export function renderResult(r, o = {}) {
  const lang = o.lang || r.lang || "hi";
  const color = ["green", "amber", "red"].includes(r.card?.color) ? r.card.color : "amber";
  if (o.phone) {
    // Phone mode: audio-first, keep only the coloured shape + title + replay.
    return html`<article class="card card--${color} card--phone" aria-labelledby="verdict-title">
      ${head(r, lang)}${replyAudioOnly(r, lang)}${foot(lang)}</article>`;
  }
  if (o.compact) {
    const hasDetails = Object.keys(r.items || {}).length || r.price;
    return html`<article class="card card--${color}" aria-labelledby="verdict-title">
      ${head(r, lang)}
      ${confirmSec(r, lang, o)}${needsImageSec(r, lang)}${errorSec(r, lang)}
      ${replySec(r, lang, true)}${flagsSec(r, lang)}${safetySec(r, lang)}
      ${r.mode === "compare" ? itemsSec(r, lang) : ""}
      ${altsSec(r, lang)}
      ${hasDetails ? html`<details class="result-details"><summary>${t(lang, "medicine")} · ${t(lang, "salts")} · ${t(lang, "price")}</summary>
        ${r.mode === "compare" ? "" : itemsSec(r, lang)}${priceSec(r, lang)}</details>` : ""}
      ${chemistSec(r, lang, o)}
      ${Object.keys(r.data_sources || {}).length ? html`<details class="result-details"><summary>${t(lang, "data")}</summary>${renderDataSources(r.data_sources, lang)}</details>` : ""}
    </article>`;
  }
  return html`<article class="card card--${color}" aria-labelledby="verdict-title">
    ${head(r, lang)}
    ${said(r, lang)}
    ${confirmSec(r, lang, o)}
    ${needsImageSec(r, lang)}
    ${errorSec(r, lang)}
    ${matchSec(r, lang)}
    ${itemsSec(r, lang)}
    ${safetySec(r, lang)}
    ${priceSec(r, lang)}
    ${altsSec(r, lang)}
    ${flagsSec(r, lang)}
    ${replySec(r, lang)}
    ${chemistSec(r, lang, o)}
    ${pouchSec(r, lang, o)}
    ${symptomsSec(r, lang)}
    ${foot(lang)}
  </article>`;
}

function replyAudioOnly(r, lang) {
  return html`<section class="sec reply">${audioRow(r, lang)}</section>`;
}

/** Card for transport / HTTP errors ({"error":{"code","message"}} or network failures). */
export function renderError(err, lang) {
  const code = err?.code || "";
  const key = code && t(lang, "err_" + code) !== "err_" + code ? "err_" + code : code === "NETWORK" ? "err_network" : "err_generic";
  return html`<article class="card card--amber" aria-labelledby="verdict-title">
    ${head({ status: "error", card: { color: "amber", icon: "question" } }, lang)}
    <section class="sec err"><p>${L(lang, key)}</p>${code ? html`<p class="small muted">${code}${err.message ? " · " + err.message : ""}</p>` : ""}</section>
    ${foot(lang)}</article>`;
}

/** STT / TTS / OCR mode chips for the header. */
export function renderModes(cfg) {
  if (!cfg) return "";
  const chip = (k, v, live) => html`<span class="mode${live ? " mode--live" : ""}" title="${k}: ${v}"><b>${k}</b> ${v}</span>`;
  return html`${chip("STT", cfg.stt, cfg.stt !== "mock")}${chip("TTS", cfg.tts, cfg.tts !== "mock")}${chip("OCR", cfg.ocr, cfg.ocr && cfg.ocr !== "mock")}`;
}

/** The "Data" footer: as_of + source for each dataset. */
export function renderDataSources(ds, lang) {
  const names = { brands: "Brands", banned_fdc: "Banned FDC list", price_ref: "Price reference (Jan Aushadhi / NPPA)", nsq_alerts: "NSQ alerts" };
  const rows = Object.entries(ds || {}).filter(([k, v]) => v && typeof v === "object" && names[k]);
  if (!rows.length) return html`<p class="small muted">—</p>`;
  return html`<dl class="data-list">${rows.map(([k, v]) => html`<div><dt>${names[k]}</dt>
    <dd><span class="data-asof">${t(lang, "data_as_of")}: ${fmtDate(v.as_of, lang) || "—"}</span>${v.rows != null ? html` · ${v.rows} rows` : ""}<br><span class="src">${v.source || ""}</span></dd></div>`)}</dl>`;
}

/** Debug drawer: timings of the last response and the OCR provider. */
export function renderDebug(r, extra = {}) {
  if (!r) return html`<p class="small muted">—</p>`;
  const tm = r.timings_ms || {};
  const keys = ["asr", "ocr", "match", "engine", "script", "tts_first_chunk", "total"];
  const ocr = r.data_sources?.ocr;
  const ocrTxt = ocr ? Object.entries(ocr).map(([k, v]) => `${k}: ${v.provider}${v.readable === false ? " (unreadable)" : ""}${v.fallback_reason ? " – " + v.fallback_reason : ""}`).join("; ") : "—";
  return html`<table class="dbg"><tbody>
    ${keys.map((k) => html`<tr><th scope="row">${k}</th><td>${tm[k] == null ? "—" : num(tm[k]) + " ms"}</td></tr>`)}
    ${Object.entries(extra).map(([k, v]) => html`<tr><th scope="row">${k}</th><td>${v == null ? "—" : v}</td></tr>`)}
    <tr><th scope="row">ocr provider</th><td>${ocrTxt}</td></tr>
    <tr><th scope="row">status</th><td>${r.status}${r.verdict ? " · " + r.verdict.code + " (" + r.verdict.mode + ")" : ""}</td></tr>
    <tr><th scope="row">intent</th><td>${r.intent || "—"}</td></tr>
    <tr><th scope="row">tts</th><td>${r.tts}${r.script && r.script.guard_ok === false ? " · guard fallback" : ""}</td></tr>
    <tr><th scope="row">session</th><td>${r.session_id || "—"}</td></tr>
  </tbody></table>`;
}
