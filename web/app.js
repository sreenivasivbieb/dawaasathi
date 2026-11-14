// Dawaa Saathi web client: language, tap-to-talk, typed questions, photos, confirmations, home
// pouch, chat history per thread, phone mode and live (WebSocket) mode.
//
// Rendering lives in render.js (pure HTML strings), audio in audio.js, UI strings in i18n.js.
// Server API: app/api/routes.py (REST), app/api/stream.py (WebSocket /v1/stream).

import {
  Mic, Vad, encodeWav, streamPcm, speakText, unlockAudio, StreamPlayer, PcmDecoder, fetchPcm, playSamples,
} from "./audio.js";
import { LANGS, LANG_NAMES, t, sub } from "./i18n.js";
import { renderResult, renderError, renderModes, renderDataSources, renderDebug, targetSalts, esc } from "./render.js";
import { LiveLink } from "./live.js";

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ persisted per-device state
const store = {
  get(k, d) { try { const v = localStorage.getItem("dawa." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("dawa." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

const state = {
  lang: LANGS.includes(store.get("lang")) ? store.get("lang") : guessLang(),
  sessionId: store.get("session", null),
  threads: store.get("threads", []), // thread ids this device started (newest first)
  pouch: store.get("pouch", []),
  phone: false,
  live: null, // LiveLink while live mode is on
  photos: { strip: null, rx: null },
  last: null, // last QueryResponse
  turns: [], // [{user, attachments, response}] of the open thread (for the bubbles)
  chemistOpen: false,
  audio: null, // current playback handle {stop()}
  cfg: null,
  busy: false,
};

function guessLang() {
  const nav = (navigator.language || "hi").slice(0, 2).toLowerCase();
  return LANGS.includes(nav) ? nav : "hi";
}

// ------------------------------------------------------------------ language
function fillLangSelect(sel) {
  sel.innerHTML = LANGS.map((l) => `<option value="${l}" lang="${l}">${esc(LANG_NAMES[l])}</option>`).join("");
  sel.value = state.lang;
  sel.addEventListener("change", () => setLang(sel.value));
}

function setLang(lang) {
  if (!LANGS.includes(lang)) return;
  state.lang = lang;
  store.set("lang", lang);
  applyLang();
  if (state.live) { stopLive(); startLive(); } // the live STT stream is per language
}

function applyLang() {
  const lang = state.lang;
  document.documentElement.lang = lang;
  for (const id of ["lang", "lang2"]) if ($(id)) $(id).value = lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(lang, el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-sub]").forEach((el) => { el.textContent = sub(lang, el.dataset.i18nSub); });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = t(lang, el.dataset.i18nPh); });
  document.querySelectorAll("[data-i18n-aria]").forEach((el) => { el.setAttribute("aria-label", t(lang, el.dataset.i18nAria)); });
  document.title = `${t(lang, "app_name")} · Dawaa Saathi`;
  setMicState($("mic").dataset.state || "idle");
  renderPouch();
  renderThread();
  if (state.last) showResult(state.last, { speak: false });
  if (state.cfg) $("data-body").innerHTML = String(renderDataSources(state.cfg.data_sources, lang));
}

// ------------------------------------------------------------------ small UI helpers
let toastTimer = 0;
function toast(key, vars) {
  const el = $("toast");
  el.textContent = t(state.lang, key, vars);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

const MIC_LABEL = { idle: "mic_idle", recording: "mic_listening", thinking: "mic_thinking", speaking: "mic_speaking", live: "mic_live" };
function setMicState(s) {
  const mic = $("mic");
  mic.dataset.state = s;
  const key = MIC_LABEL[s] || "mic_idle";
  const st = $("mic-status");
  st.innerHTML = `<span class="lbl">${esc(t(state.lang, key))}</span>${state.lang === "en" ? "" : `<span class="en">${esc(sub(state.lang, key))}</span>`}`;
  mic.setAttribute("aria-label", t(state.lang, s === "recording" ? "mic_aria_stop" : "mic_aria_start"));
}

function stopAudio() {
  try { state.audio?.stop(); } catch { /* already stopped */ }
  state.audio = null;
  if ($("mic").dataset.state === "speaking") setMicState(state.live ? "live" : "idle");
}

// ------------------------------------------------------------------ results + conversation thread
function showResult(r, { speak = true } = {}) {
  state.last = r;
  $("result").innerHTML = String(renderResult(r, {
    lang: state.lang, phone: state.phone, pouch: state.pouch, chemistOpen: state.chemistOpen, sessionId: state.sessionId,
  }));
  $("debug-body").innerHTML = String(renderDebug(r));
  if (r.data_sources && state.cfg) state.cfg.data_sources = { ...state.cfg.data_sources, ...r.data_sources };
  if (speak) play(r);
}

function renderThread() {
  // Every finished turn except the newest one (that one is the full card below).
  const turns = state.turns.slice(0, -1);
  const lang = state.lang;
  $("thread").innerHTML = turns.map((tn) => {
    const r = tn.response || {};
    const tags = (tn.attachments || []).filter((a) => a !== "audio" || !tn.user)
      .map((a) => `<span class="tag">[${esc(t(lang, a === "audio" ? "attached_audio" : "attached_photo"))}]</span>`).join("");
    const user = tn.user || tags ? `<p class="bubble bubble--user" lang="${esc(r.lang || lang)}">${tags}${esc(tn.user || "")}</p>` : "";
    const color = ["green", "amber", "red"].includes(r.card?.color) ? r.card.color : "amber";
    const bot = r.script?.text ? `<p class="bubble bubble--bot" lang="${esc(r.lang || lang)}"><span class="tone tone--${color}" aria-hidden="true"></span>${esc(r.script.text)}</p>` : "";
    return `<div class="turn">${user}${bot}</div>`;
  }).join("");
}

function pushTurn(user, attachments, response) {
  state.turns.push({ user, attachments, response });
  renderThread();
}

function rememberThread(id) {
  if (!id) return;
  state.sessionId = id;
  store.set("session", id);
  if (!state.threads.includes(id)) {
    state.threads = [id, ...state.threads].slice(0, 50);
    store.set("threads", state.threads);
  }
}

// ------------------------------------------------------------------ playback
function play(r) {
  stopAudio();
  const text = r.script?.text;
  const onstart = () => setMicState("speaking");
  const onend = () => { if (state.audio) state.audio = null; setMicState(state.live ? "live" : "idle"); };
  if (r.tts === "timbre" && r.audio_stream_url) {
    const ctl = streamPcm(r.audio_stream_url, { onstart, onended: onend, onerror: onend });
    state.audio = ctl;
  } else if (text) {
    state.audio = speakText(r.script.speech_text || text, r.lang || state.lang, { onstart, onend });
  }
}

async function playChemist() {
  if (!state.sessionId) return;
  try {
    const res = await api("/v1/speak", { json: { session_id: state.sessionId, which: "chemist", lang: state.lang } });
    stopAudio();
    if (res.tts === "timbre") state.audio = streamPcm(res.audio_stream_url, {});
    else state.audio = speakText(res.text, state.lang, {});
  } catch (e) { showError(e); }
}

// ------------------------------------------------------------------ server calls
async function api(path, { form, json, method } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method: method || (form || json ? "POST" : "GET"),
      body: form || (json ? JSON.stringify(json) : undefined),
      headers: json ? { "Content-Type": "application/json" } : undefined,
    });
  } catch {
    throw { code: "NETWORK" };
  }
  let body = null;
  try { body = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw body?.error || { code: `HTTP_${res.status}` };
  return body;
}

function showError(err) {
  setMicState(state.live ? "live" : "idle");
  $("result").innerHTML = String(renderError(err, state.lang));
}

let filler = null;
async function playFiller() {
  try {
    if (!filler || filler.lang !== state.lang) {
      const pcm = await fetchPcm(`/v1/audio/filler?lang=${state.lang}`);
      filler = pcm ? { ...pcm, lang: state.lang } : { lang: state.lang, samples: null };
    }
    if (filler.samples && state.busy) return playSamples(filler.samples, filler.sr, {});
  } catch { /* the filler line is optional */ }
  return null;
}

async function ask({ text, wav, label }) {
  if (state.busy) return;
  const photos = { ...state.photos };
  if (!text && !wav && !photos.strip && !photos.rx) { toast("err_EMPTY_REQUEST"); return; }
  state.busy = true;
  stopAudio();
  setMicState("thinking");
  const fillerHandle = state.cfg?.tts === "timbre" ? playFiller() : null;
  const fd = new FormData();
  fd.set("lang", state.lang);
  fd.set("channel", state.phone ? "phone" : "web");
  if (state.sessionId) fd.set("session_id", state.sessionId);
  if (state.pouch.length) fd.set("pouch", JSON.stringify(state.pouch));
  if (text) fd.set("text", text);
  if (wav) fd.set("audio", new Blob([wav], { type: "audio/wav" }), "speech.wav");
  if (photos.strip) fd.set("strip", photos.strip, photos.strip.name || "strip.jpg");
  if (photos.rx) fd.set("rx", photos.rx, photos.rx.name || "rx.jpg");
  try {
    const r = await api("/v1/query", { form: fd });
    (await fillerHandle)?.stop?.();
    rememberThread(r.session_id);
    clearPhotos();
    state.chemistOpen = false;
    const attachments = [wav && "audio", photos.strip && "strip", photos.rx && "rx"].filter(Boolean);
    pushTurn(text || label || r.transcript || "", attachments, r);
    showResult(r);
  } catch (err) {
    (await fillerHandle)?.stop?.();
    showError(err);
  } finally {
    state.busy = false;
    if ($("mic").dataset.state === "thinking") setMicState(state.live ? "live" : "idle");
  }
}

async function confirmChoice(candidateId, none) {
  if (!state.sessionId || state.busy) return;
  state.busy = true;
  setMicState("thinking");
  try {
    const r = await api("/v1/confirm", { json: { session_id: state.sessionId, candidate_id: candidateId, none: !!none, lang: state.lang } });
    const label = none ? t(state.lang, "none_of_these")
      : (state.last?.candidates || []).find((c) => c.candidate_id === candidateId)?.label || "";
    pushTurn(label, [], r);
    showResult(r);
  } catch (err) { showError(err); } finally {
    state.busy = false;
    if ($("mic").dataset.state === "thinking") setMicState("idle");
  }
}

// ------------------------------------------------------------------ tap-to-talk
const mic = new Mic();
const rec = { on: false, frames: [], vad: null, heard: false, ms: 0 };

async function toggleRecording() {
  unlockAudio();
  if (state.live) { state.live.json({ type: "commit" }); return; }
  if (rec.on) return finishRecording();
  if ($("mic").dataset.state === "speaking") { stopAudio(); }
  const rate = state.phone ? 8000 : 16000;
  try {
    await mic.start(rate);
  } catch (e) {
    toast(e?.name === "NotAllowedError" ? "mic_denied" : "mic_unsupported");
    return;
  }
  Object.assign(rec, { on: true, frames: [], vad: new Vad(), heard: false, ms: 0 });
  mic.onframe = (pcm, rms, ms) => {
    if (!rec.on) return;
    rec.frames.push(pcm);
    rec.ms += ms;
    document.documentElement.style.setProperty("--level", Math.min(1, rms * 12).toFixed(3));
    const ev = rec.vad.push(rms, ms);
    if (ev === "start") rec.heard = true;
    // Stop by itself after the speaker pauses, or after 20 s at most.
    if ((ev === "end" && rec.heard) || rec.ms > 20000) finishRecording();
  };
  setMicState("recording");
}

function finishRecording() {
  if (!rec.on) return;
  rec.on = false;
  mic.onframe = null;
  mic.stop();
  if (!rec.heard || rec.ms < 400) { setMicState("idle"); toast("no_speech"); return; }
  const wav = encodeWav(rec.frames, mic.targetRate);
  rec.frames = [];
  ask({ wav });
}

// ------------------------------------------------------------------ live mode (WebSocket)
const liveVad = new Vad({ factor: 4, hangMs: 500 });
let livePlayer = null;

async function startLive() {
  try {
    await mic.start(16000);
  } catch (e) {
    $("tg-live").checked = false;
    toast(e?.name === "NotAllowedError" ? "mic_denied" : "mic_unsupported");
    return;
  }
  const link = new LiveLink({
    onjson: onLiveMessage,
    onbinary: (bytes) => livePlayer?.push(livePlayer.decoder.push(bytes)),
    onclose: () => { if (state.live === link) { state.live = null; $("tg-live").checked = false; mic.stop(); setMicState("idle"); toast("live_lost"); } },
  });
  state.live = link;
  link.connect({ lang: state.lang, session_id: state.sessionId, pouch: state.pouch, sample_rate: 16000,
                 channel: state.phone ? "phone" : "web" });
  liveVad.reset();
  mic.onframe = (pcm, rms, ms) => {
    link.pcm(pcm);
    document.documentElement.style.setProperty("--level", Math.min(1, rms * 12).toFixed(3));
    if (liveVad.push(rms, ms) === "start" && livePlayer?.active) { // barge-in
      link.json({ type: "interrupt" });
      livePlayer.stop();
    }
  };
  setMicState("live");
}

function stopLive() {
  const link = state.live;
  state.live = null;
  link?.close();
  livePlayer?.stop();
  mic.onframe = null;
  mic.stop();
  setMicState("idle");
}

function onLiveMessage(msg) {
  switch (msg.type) {
    case "ready":
      rememberThread(msg.session_id);
      toast(msg.stt === "mock" ? "live_no_stt" : "live_on");
      break;
    case "transcript":
      $("live-line").hidden = false;
      $("live-line").textContent = msg.text;
      break;
    case "result": {
      const { type, ...r } = msg;
      rememberThread(r.session_id);
      pushTurn(r.transcript || "", [], r);
      showResult(r, { speak: r.tts !== "timbre" }); // live Timbre audio arrives on the socket
      break;
    }
    case "audio_start":
      livePlayer?.stop();
      livePlayer = new StreamPlayer(msg.sample_rate, { onstart: () => setMicState("speaking"), onended: () => setMicState("live") });
      livePlayer.decoder = new PcmDecoder();
      break;
    case "audio_end":
      livePlayer?.end();
      break;
    case "error":
      if (msg.code === "STT_NOT_CONFIGURED" || msg.code === "STT_STREAM_UNAVAILABLE") toast("live_no_stt");
      break;
    default:
  }
}

// ------------------------------------------------------------------ photos
function setPhoto(kind, file) {
  if (file && state.cfg && file.size > 10 * 1024 * 1024) { toast("err_TOO_LARGE"); return; }
  state.photos[kind] = file || null;
  renderThumbs();
  if (file && state.live) {
    const fr = new FileReader();
    fr.onload = () => state.live?.json({ type: "image", kind, data: String(fr.result).split(",")[1] });
    fr.readAsDataURL(file);
  }
}

function clearPhotos() {
  state.photos = { strip: null, rx: null };
  $("in-strip").value = "";
  $("in-rx").value = "";
  renderThumbs();
}

function renderThumbs() {
  const lang = state.lang;
  const items = Object.entries(state.photos).filter(([, f]) => f);
  for (const old of $("thumbs").querySelectorAll("img")) URL.revokeObjectURL(old.src);
  $("thumbs").innerHTML = items.map(([kind, f]) => `<figure class="thumb"><img src="${URL.createObjectURL(f)}" alt="">
    <figcaption>${esc(t(lang, kind === "strip" ? "strip_photo" : "rx_photo"))}</figcaption>
    <button class="thumb__x iconbtn" type="button" data-act="unphoto" data-kind="${kind}" aria-label="${esc(t(lang, "remove"))}">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg></button></figure>`).join("");
  $("btn-check-photo").hidden = !items.length;
  $("photo-hint").hidden = !items.length;
  $("photo-hint").textContent = items.length ? t(lang, "photo_note") : "";
}

// ------------------------------------------------------------------ home pouch (stays on the device)
function renderPouch() {
  const lang = state.lang;
  $("pouch-count").hidden = !state.pouch.length;
  $("pouch-count").textContent = String(state.pouch.length);
  $("pouch-list").innerHTML = state.pouch.length
    ? state.pouch.map((s, i) => `<li><span>${esc(s)}</span><button class="iconbtn" type="button" data-act="pouch-del" data-i="${i}" aria-label="${esc(t(lang, "remove"))}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg></button></li>`).join("")
    : `<li class="empty">${esc(t(lang, "pouch_empty"))}</li>`;
}

function setPouch(list) {
  state.pouch = [...new Set(list.map((s) => s.trim().toLowerCase()).filter(Boolean))].slice(0, 50);
  store.set("pouch", state.pouch);
  renderPouch();
  state.live?.json({ type: "pouch", pouch: state.pouch });
}

// ------------------------------------------------------------------ chat history
async function openHistory() {
  const lang = state.lang;
  $("history-list").innerHTML = `<li class="empty">…</li>`;
  $("history").showModal();
  let threads = [];
  try {
    if (state.threads.length) threads = (await api(`/v1/threads?ids=${encodeURIComponent(state.threads.join(","))}`)).threads;
  } catch { /* shown as empty */ }
  // Forget ids the server no longer has (deleted elsewhere).
  const known = new Set(threads.map((x) => x.id));
  state.threads = state.threads.filter((id) => known.has(id) || id === state.sessionId);
  store.set("threads", state.threads);
  $("history-list").innerHTML = threads.length ? threads.map((th) => {
    const when = new Date(th.updated_at).toLocaleString(lang === "en" ? "en-IN" : `${lang}-IN-u-nu-latn`, { dateStyle: "medium", timeStyle: "short" });
    return `<li class="${th.id === state.sessionId ? "current" : ""}">
      <button class="history-open" type="button" data-act="thread-open" data-id="${esc(th.id)}" lang="${esc(th.lang || lang)}">
        <b>${esc(th.title || "—")}</b><span>${esc(when)} · ${esc(LANG_NAMES[th.lang] || "")} · ${esc(t(lang, "history_messages", { n: th.messages }))}</span></button>
      <button class="iconbtn" type="button" data-act="thread-del" data-id="${esc(th.id)}" aria-label="${esc(t(lang, "history_delete"))}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M10 7V5h4v2M7 7l1 13h8l1-13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></li>`;
  }).join("") : `<li class="empty">${esc(t(lang, "history_empty"))}</li>`;
}

async function loadThread(id, { quiet = false } = {}) {
  try {
    const th = await api(`/v1/threads/${encodeURIComponent(id)}`);
    const turns = [];
    let pending = null;
    for (const m of th.messages) {
      if (m.role === "user") pending = m;
      else { turns.push({ user: pending?.text || "", attachments: pending?.attachments || [], response: m.response || {} }); pending = null; }
    }
    rememberThread(id);
    state.turns = turns;
    if (th.lang && LANGS.includes(th.lang) && th.lang !== state.lang) { state.lang = th.lang; store.set("lang", th.lang); }
    state.last = null;
    $("result").innerHTML = "";
    applyLang();
    const lastResp = turns.at(-1)?.response;
    if (lastResp) showResult(lastResp, { speak: false });
    return true;
  } catch (err) {
    if (!quiet) showError(err);
    if (err?.code === "THREAD_NOT_FOUND" && id === state.sessionId) newChat({ quiet: true });
    return false;
  }
}

async function deleteThread(id) {
  try {
    await api(`/v1/threads/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch { /* already gone */ }
  state.threads = state.threads.filter((x) => x !== id);
  store.set("threads", state.threads);
  if (id === state.sessionId) newChat({ quiet: true });
  toast("history_deleted");
  openHistory();
}

function newChat({ quiet = false } = {}) {
  stopAudio();
  if (state.live) stopLive();
  state.sessionId = null;
  store.set("session", null);
  state.turns = [];
  state.last = null;
  clearPhotos();
  $("thread").innerHTML = "";
  $("result").innerHTML = "";
  $("live-line").hidden = true;
  if (!quiet) toast("new_done");
}

// ------------------------------------------------------------------ events
function onAction(e) {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const act = el.dataset.act;
  unlockAudio();
  switch (act) {
    case "confirm": return confirmChoice(el.dataset.id, false);
    case "confirm-none": return confirmChoice(null, true);
    case "pick": return $(el.dataset.kind === "rx" ? "in-rx" : "in-strip").click();
    case "replay": return state.last && play(state.last);
    case "stop-audio": return stopAudio();
    case "chemist-toggle":
      state.chemistOpen = !state.chemistOpen;
      return state.last && showResult(state.last, { speak: false });
    case "chemist-play": return playChemist();
    case "pouch-add":
      setPouch([...state.pouch, ...targetSalts(state.last || {})]);
      return state.last && showResult(state.last, { speak: false });
    case "pouch-del": return setPouch(state.pouch.filter((_, i) => i !== Number(el.dataset.i)));
    case "symptom": return ask({ text: el.dataset.text, label: el.textContent.trim() });
    case "unphoto": return setPhoto(el.dataset.kind, null);
    case "thread-open": $("history").close(); return loadThread(el.dataset.id);
    case "thread-del": return deleteThread(el.dataset.id);
    default:
  }
}

function bind() {
  fillLangSelect($("lang"));
  fillLangSelect($("lang2"));
  $("mic").addEventListener("click", toggleRecording);
  $("ask").addEventListener("submit", (e) => {
    e.preventDefault();
    unlockAudio();
    const q = $("q").value.trim();
    if (!q && !state.photos.strip && !state.photos.rx) return;
    $("q").value = "";
    if (state.live && q) { state.live.json({ type: "text", text: q }); return; }
    ask({ text: q || null });
  });
  $("in-strip").addEventListener("change", (e) => setPhoto("strip", e.target.files[0]));
  $("in-rx").addEventListener("change", (e) => setPhoto("rx", e.target.files[0]));
  $("btn-check-photo").addEventListener("click", () => {
    unlockAudio();
    if (state.live) state.live.json({ type: "text", text: "" });
    else ask({});
  });
  document.body.addEventListener("click", onAction);
  $("btn-new").addEventListener("click", () => newChat());
  $("btn-history").addEventListener("click", openHistory);
  $("history-close").addEventListener("click", () => $("history").close());
  $("btn-pouch").addEventListener("click", () => $("pouch").showModal());
  $("pouch-close").addEventListener("click", () => $("pouch").close());
  $("pouch-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("pouch-in").value.trim();
    if (v) setPouch([...state.pouch, v]);
    $("pouch-in").value = "";
  });
  $("tg-phone").addEventListener("change", (e) => {
    state.phone = e.target.checked;
    if (state.last) showResult(state.last, { speak: false });
  });
  $("tg-live").addEventListener("change", (e) => (e.target.checked ? startLive() : (stopLive(), toast("live_off"))));
  for (const d of [$("pouch"), $("history")]) {
    d.addEventListener("click", (e) => { if (e.target === d) d.close(); }); // tap outside closes
  }
}

async function init() {
  bind();
  applyLang();
  if (!store.get("consent", false)) {
    const dlg = $("consent");
    dlg.addEventListener("close", () => store.set("consent", true), { once: true });
    dlg.showModal();
  }
  try {
    state.cfg = await api("/v1/config");
    $("modes").innerHTML = String(renderModes(state.cfg));
    $("data-body").innerHTML = String(renderDataSources(state.cfg.data_sources, state.lang));
  } catch { /* offline: the page still works for typing once the network is back */ }
  if (state.sessionId) await loadThread(state.sessionId, { quiet: true });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
}

init();
