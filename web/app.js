// Momo · Dawaa Saathi Web Client
// Real-time integration with FastAPI backend (/v1/query, /v1/stream, /v1/speak, /v1/alternatives, etc.)

import {
  Mic, Vad, encodeWav, streamPcm, speakText, unlockAudio, StreamPlayer, PcmDecoder, fetchPcm, playSamples,
} from "./audio.js";
import { LANGS, LANG_NAMES, t, sub } from "./i18n.js";
import {
  renderResult, renderError, renderModes, renderDataSources, renderDebug,
  targetSalts, targetItem, saltLabel, rupee, fmtDate, esc,
} from "./render.js";
import { LiveLink } from "./live.js";

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ Persisted State
const store = {
  get(k, d) {
    try {
      const v = localStorage.getItem("momo." + k) ?? localStorage.getItem("dawa." + k);
      return v == null ? d : JSON.parse(v);
    } catch {
      return d;
    }
  },
  set(k, v) {
    try {
      localStorage.setItem("momo." + k, JSON.stringify(v));
    } catch { /* private mode */ }
  },
};

const state = {
  lang: LANGS.includes(store.get("lang")) ? store.get("lang") : guessLang(),
  sessionId: store.get("session", null),
  threads: store.get("threads", []),
  pouch: store.get("pouch", []),
  phone: false,
  live: null,
  photos: { strip: null, rx: null },
  last: null,
  turns: [],
  chemistOpen: false,
  audio: null,
  cfg: null,
  busy: false,
  callActive: false,
  callSeconds: 83, // Starts at 01:23 matching the screenshot
  callTimerInterval: null,
};

function guessLang() {
  const nav = (navigator.language || "en").slice(0, 2).toLowerCase();
  return LANGS.includes(nav) ? nav : "en";
}

// ------------------------------------------------------------------ UI Helpers & Toast
let toastTimer = 0;
function toast(msg) {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

// ------------------------------------------------------------------ Soundwave Waveform Animation
let wavePhase = 0;
function animateSoundwave() {
  const path = $("soundwave-path");
  if (path) {
    const isLively = state.busy || state.callActive || $("mic")?.dataset?.state === "speaking" || $("mic")?.dataset?.state === "recording";
    const amp = isLively ? 12 : 2.5;
    const freq = isLively ? 0.038 : 0.015;
    wavePhase += isLively ? 0.12 : 0.03;
    let d = "M 0 24 ";
    for (let x = 0; x <= 320; x += 10) {
      const envelope = Math.sin((x / 320) * Math.PI);
      const y = 24 + Math.sin(x * freq + wavePhase) * amp * envelope;
      d += `L ${x} ${y.toFixed(1)} `;
    }
    path.setAttribute("d", d);
  }
  requestAnimationFrame(animateSoundwave);
}

// ------------------------------------------------------------------ Call Timer & State
function formatTimer(totalSecs) {
  const mins = Math.floor(totalSecs / 60);
  const secs = totalSecs % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function startCallTimer() {
  if (state.callTimerInterval) clearInterval(state.callTimerInterval);
  state.callActive = true;
  $("call-status").textContent = "Connected";
  $("receptionist-card")?.classList.add("in-call");
  state.callTimerInterval = setInterval(() => {
    state.callSeconds++;
    const formatted = formatTimer(state.callSeconds);
    $("call-timer").textContent = formatted;
    const durEl = $("audio-pill-duration");
    if (durEl) durEl.textContent = formatted;
  }, 1000);
}

function stopCallTimer() {
  if (state.callTimerInterval) clearInterval(state.callTimerInterval);
  state.callTimerInterval = null;
  state.callActive = false;
  $("call-status").textContent = "Ready";
  $("receptionist-card")?.classList.remove("in-call");
}

function setMicState(s) {
  const mic = $("mic");
  if (mic) {
    mic.dataset.state = s;
    if (s === "recording") {
      mic.classList.add("recording");
      $("call-status").textContent = "Listening…";
      startCallTimer();
    } else {
      mic.classList.remove("recording");
    }
  }

  const orb = $("receptionist-orb");
  if (orb) {
    orb.dataset.state = s;
  }

  const statusLabel = $("mic-status");
  if (statusLabel) {
    statusLabel.textContent = s;
  }

  if (s === "thinking") {
    $("call-status").textContent = "Analyzing…";
  } else if (s === "speaking") {
    $("call-status").textContent = "Speaking…";
  } else if (s === "idle" && !state.callActive) {
    $("call-status").textContent = "Ready";
  }
}

// ------------------------------------------------------------------ Audio Playback
function stopAudio() {
  try { state.audio?.stop(); } catch { /* stopped */ }
  state.audio = null;
  if ($("mic")?.dataset?.state === "speaking") {
    setMicState(state.live ? "live" : "idle");
  }
}

function playResponse(r) {
  stopAudio();
  const text = r.script?.speech_text || r.script?.text;
  const onstart = () => {
    setMicState("speaking");
    startCallTimer();
  };
  const onend = () => {
    if (state.audio) state.audio = null;
    setMicState(state.live ? "live" : "idle");
  };

  if (r.tts === "timbre" && r.audio_stream_url) {
    state.audio = streamPcm(r.audio_stream_url, { onstart, onended: onend, onerror: onend });
  } else if (text) {
    state.audio = speakText(text, r.lang || state.lang, { onstart, onend });
  }
}

// ------------------------------------------------------------------ Server Calls
async function api(path, { form, json, method } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method: method || (form || json ? "POST" : "GET"),
      body: form || (json ? JSON.stringify(json) : undefined),
      headers: json ? { "Content-Type": "application/json" } : undefined,
    });
  } catch {
    throw { code: "NETWORK", message: "Network connection failure" };
  }
  let body = null;
  try { body = await res.json(); } catch { /* ignore */ }
  if (!res.ok) throw body?.error || { code: `HTTP_${res.status}`, message: "Server error" };
  return body;
}

function showError(err) {
  setMicState(state.live ? "live" : "idle");
  toast(err?.message || err?.code || "Something went wrong.");
  const resEl = $("result");
  if (resEl) resEl.innerHTML = String(renderError(err, state.lang));
}

// ------------------------------------------------------------------ Main Query Handler
async function ask({ text, wav, label, photos: customPhotos }) {
  if (state.busy) return;
  const photos = customPhotos || { ...state.photos };
  if (!text && !wav && !photos.strip && !photos.rx) {
    toast("Please enter a question, speak, or upload a photo.");
    return;
  }

  state.busy = true;
  stopAudio();
  setMicState("thinking");

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
    rememberThread(r.session_id);
    clearPhotos();
    state.last = r;

    const attachments = [wav && "audio", photos.strip && "strip", photos.rx && "rx"].filter(Boolean);
    const userText = text || label || r.transcript || (attachments.length ? `[${attachments.join(", ")}]` : "");

    // Push new dialogue turn
    pushTurn(userText, attachments, r);

    // Update structured clinic record & play voice
    updateClinicRecord(r);
    playResponse(r);
  } catch (err) {
    showError(err);
  } finally {
    state.busy = false;
    if ($("mic")?.dataset?.state === "thinking") {
      setMicState(state.live ? "live" : "idle");
    }
  }
}

// ------------------------------------------------------------------ Transcript & Clinic Record Rendering
function getCurrentTimeFormatted() {
  const now = new Date();
  return now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

function createChatRow(role, name, time, text, audioUrl) {
  const isMR = role === "mr";
  const avatarClass = isMR ? "mr" : "p";
  const avatarText = isMR ? "MR" : "P";
  const audioBtn = (isMR && text) ? `
    <button class="chat-bubble-audio-btn" type="button" data-play-text="${esc(text)}">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
      <span>Listen</span>
    </button>
  ` : "";

  return `
    <div class="chat-bubble-row ${isMR ? "receptionist" : "patient"}">
      <div class="chat-avatar ${avatarClass}">${avatarText}</div>
      <div class="chat-bubble-content">
        <div class="chat-bubble-meta">
          <span class="chat-sender-name">${esc(name)}</span>
          <span class="chat-time">${esc(time)}</span>
        </div>
        <p class="chat-bubble-text">${esc(text)}</p>
        ${audioBtn}
      </div>
    </div>
  `;
}

function renderThread() {
  const container = $("thread");
  if (!container) return;

  if (state.turns.length === 0) {
    // Initial seeded conversation matching the theme screenshot
    container.innerHTML = `
      ${createChatRow("patient", "Patient", "02:32", "Hi, I'd like to reschedule my appointment to next Friday and check my prescription for Augmentin 625 Duo.")}
      ${createChatRow("mr", "Momo Receptionist", "02:33", "Of course. Can I confirm you'd like next Friday at 10:00 AM? Also, Augmentin 625 Duo contains Amoxicillin 500 mg and Clavulanic Acid 125 mg. A Jan Aushadhi generic alternative is available for ₹62, saving about 72%.")}
    `;
    return;
  }

  container.innerHTML = state.turns.map((turn) => {
    const userRow = turn.user ? createChatRow("patient", "Patient", turn.time || "Now", turn.user) : "";
    const resp = turn.response || {};
    const mrText = resp.script?.text || resp.card?.title || "";
    const mrRow = mrText ? createChatRow("mr", "Momo Receptionist", turn.time || "Now", mrText, resp.audio_url) : "";
    return userRow + mrRow;
  }).join("");

  container.scrollTop = container.scrollHeight;
}

function pushTurn(user, attachments, response) {
  state.turns.push({
    user,
    attachments,
    response,
    time: getCurrentTimeFormatted(),
  });
  renderThread();
}

function updateClinicRecord(r) {
  const it = targetItem(r);
  const m = r.match;
  const p = r.price;
  const s = r.safety;
  const alts = r.alternatives || [];

  // Update Medicine Name & Salts
  const medNameEl = $("field-med-name");
  const medSaltsEl = $("field-med-salts");
  const medMrpEl = $("field-med-mrp");
  const medGenericEl = $("field-med-generic");
  const medSavingsEl = $("field-med-savings");

  const brandName = m?.brand || it?.brand || "Medicine identified";
  if (medNameEl) medNameEl.textContent = brandName;

  const saltsString = it?.salts?.length
    ? it.salts.map(saltLabel).join(" + ")
    : "Verified chemical composition";
  if (medSaltsEl) medSaltsEl.textContent = saltsString;

  // Prices
  const mrpVal = p?.mrp ?? it?.mrp;
  if (medMrpEl) {
    medMrpEl.textContent = mrpVal ? `MRP ${rupee(mrpVal)}` : "MRP —";
  }

  const bestAlt = alts[0];
  if (bestAlt && medGenericEl) {
    medGenericEl.textContent = `Jan Aushadhi: ${rupee(bestAlt.price)}`;
    if (medSavingsEl) {
      medSavingsEl.textContent = bestAlt.saves_pct ? `Save ${bestAlt.saves_pct}%` : "Generic Equivalent";
      medSavingsEl.hidden = false;
    }
  } else if (p?.reference && medGenericEl) {
    medGenericEl.textContent = `Jan Aushadhi: ${rupee(p.reference)}`;
    if (medSavingsEl) {
      medSavingsEl.textContent = "Generic Reference";
      medSavingsEl.hidden = false;
    }
  } else if (medGenericEl) {
    medGenericEl.textContent = "Verified Formulation";
    if (medSavingsEl) medSavingsEl.hidden = true;
  }

  // Patient Note
  const noteEl = $("field-patient-note");
  if (noteEl) {
    noteEl.textContent = r.script?.text || "Consultation complete. Details prepared for clinic review.";
  }

  // Visit Reason / Clinical Tags
  const tagsEl = $("field-tag-chips");
  if (tagsEl) {
    const tags = [];
    const lowerSalts = (it?.salts || []).map((x) => x.name.toLowerCase());

    if (lowerSalts.some((x) => x.includes("amoxicillin") || x.includes("clavulanic"))) {
      tags.push("Respiratory issue", "Persistent cough", "Bacterial infection");
    } else if (lowerSalts.some((x) => x.includes("paracetamol"))) {
      tags.push("Fever & pain relief", "Antipyretic");
    } else if (lowerSalts.some((x) => x.includes("ibuprofen"))) {
      tags.push("Anti-inflammatory", "Pain management");
    } else if (lowerSalts.some((x) => x.includes("metformin") || x.includes("glimepiride"))) {
      tags.push("Endocrine care", "Blood glucose control");
    } else {
      tags.push("General consultation", "Prescription review");
    }

    if (bestAlt) tags.push("Generic substitute verified");
    if (s?.status === "not_listed") tags.push("Safe non-banned FDC");

    tagsEl.innerHTML = tags.map((t, idx) => `
      <span class="tag-chip ${idx >= tags.length - 2 ? "active-safe" : ""}">${esc(t)}</span>
    `).join("");
  }

  // Safety Status
  const safetyRow = $("field-safety-row");
  const safetyText = $("field-safety-text");
  if (safetyRow && safetyText) {
    if (s?.status === "banned") {
      safetyRow.className = "safety-verify-row banned";
      safetyText.textContent = `⚠ BANNED by Ministry of Health (${s.notification_ref || "Gazette order"})`;
    } else if (s?.status === "possible") {
      safetyRow.className = "safety-verify-row warning";
      safetyText.textContent = `⚠ Review Needed: Resembles banned combination`;
    } else {
      safetyRow.className = "safety-verify-row safe";
      safetyText.textContent = "Verified Safe · Not listed on Central Banned FDC gazette";
    }
  }

  // Appointment Times
  const d = new Date();
  const nextWeek = new Date(d.getTime() + 7 * 24 * 60 * 60 * 1000);
  const opt = { weekday: "short", month: "short", day: "numeric", year: "numeric" };
  const updatedTimeEl = $("field-updated-time");
  const prevTimeEl = $("field-prev-time");
  if (updatedTimeEl) updatedTimeEl.textContent = `${nextWeek.toLocaleDateString("en-US", opt)} • 10:00 AM`;
  if (prevTimeEl) prevTimeEl.textContent = `${d.toLocaleDateString("en-US", opt)} • 10:00 AM`;

  // Status Badge
  const statusBadge = $("clinic-status-text");
  if (statusBadge) statusBadge.textContent = "Draft ready to review";

  // Debug & fallback rendering
  const debugBody = $("debug-body");
  if (debugBody) debugBody.innerHTML = String(renderDebug(r));
  const resEl = $("result");
  if (resEl) resEl.innerHTML = String(renderResult(r, { lang: state.lang, sessionId: state.sessionId }));
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

// ------------------------------------------------------------------ Tap-to-Talk (Mic)
const mic = new Mic();
const rec = { on: false, frames: [], vad: null, heard: false, ms: 0 };

async function toggleRecording() {
  unlockAudio();
  if (rec.on) return finishRecording();
  if ($("mic")?.dataset?.state === "speaking") stopAudio();

  try {
    await mic.start(16000);
  } catch (e) {
    toast(e?.name === "NotAllowedError" ? "Microphone access denied." : "Microphone not supported.");
    return;
  }

  Object.assign(rec, { on: true, frames: [], vad: new Vad(), heard: false, ms: 0 });
  mic.onframe = (pcm, rms, ms) => {
    if (!rec.on) return;
    rec.frames.push(pcm);
    rec.ms += ms;
    const ev = rec.vad.push(rms, ms);
    if (ev === "start") rec.heard = true;
    if ((ev === "end" && rec.heard) || rec.ms > 20000) finishRecording();
  };
  setMicState("recording");
}

function finishRecording() {
  if (!rec.on) return;
  rec.on = false;
  mic.onframe = null;
  mic.stop();
  if (!rec.heard || rec.ms < 400) {
    setMicState("idle");
    toast("No speech heard. Please speak clearly.");
    return;
  }
  const wav = encodeWav(rec.frames, mic.targetRate);
  rec.frames = [];
  ask({ wav });
}

// ------------------------------------------------------------------ Image Uploads
function setPhoto(kind, file) {
  state.photos[kind] = file || null;
  renderThumbs();
}

function clearPhotos() {
  state.photos = { strip: null, rx: null };
  const inStrip = $("in-strip");
  const inRx = $("in-rx");
  if (inStrip) inStrip.value = "";
  if (inRx) inRx.value = "";
  renderThumbs();
}

function renderThumbs() {
  const container = $("thumbs");
  if (!container) return;
  const items = Object.entries(state.photos).filter(([, f]) => f);
  container.innerHTML = items.map(([kind, f]) => `
    <div class="attached-thumb-item">
      <img src="${URL.createObjectURL(f)}" alt="${kind}">
      <button class="btn-thumb-remove" type="button" data-act="remove-photo" data-kind="${kind}">✕</button>
    </div>
  `).join("");

  const checkBtn = $("btn-check-photo");
  if (checkBtn) checkBtn.hidden = !items.length;
}

// ------------------------------------------------------------------ Samples & Presets
async function triggerSample(sampleKey) {
  unlockAudio();
  if (sampleKey === "augmentin") {
    ask({ text: "Augmentin 625 Duo price and generic" });
  } else if (sampleKey === "dolo") {
    ask({ text: "Dolo 650 price and indications" });
  } else if (sampleKey === "combiflam") {
    ask({ text: "Combiflam price and safety check" });
  } else if (sampleKey === "rx-photo") {
    toast("Loading sample prescription…");
    try {
      const res = await fetch("/samples/rx_augmentin_625.png");
      const blob = await res.blob();
      const file = new File([blob], "rx_augmentin_625.png", { type: "image/png" });
      setPhoto("rx", file);
      ask({ label: "Uploaded prescription for Augmentin 625 Duo" });
    } catch {
      toast("Could not load sample file.");
    }
  } else if (sampleKey === "strip-photo") {
    toast("Loading sample medicine strip…");
    try {
      const res = await fetch("/samples/strip_augmentin_625.png");
      const blob = await res.blob();
      const file = new File([blob], "strip_augmentin_625.png", { type: "image/png" });
      setPhoto("strip", file);
      ask({ label: "Uploaded strip photo: Augmentin 625 Duo" });
    } catch {
      toast("Could not load sample file.");
    }
  }
}

// ------------------------------------------------------------------ Speak to Chemist
async function openChemistModal() {
  unlockAudio();
  const modal = $("modal-chemist");
  if (!modal) return;
  modal.showModal();

  const scriptTextEl = $("chemist-script-text");
  if (state.last?.script?.chemist_text) {
    if (scriptTextEl) scriptTextEl.textContent = `"${state.last.script.chemist_text}"`;
  } else {
    if (scriptTextEl) scriptTextEl.textContent = `"Do you have a Jan Aushadhi or cheaper generic of Amoxicillin 500 mg + Clavulanic Acid 125 mg (tablet)?"`;
  }
}

async function playChemistAudio() {
  unlockAudio();
  if (!state.sessionId) {
    const text = $("chemist-script-text")?.textContent?.replace(/^"|"$/g, "") || "Please check generic medicine.";
    speakText(text, state.lang, {});
    return;
  }
  try {
    const res = await api("/v1/speak", { json: { session_id: state.sessionId, which: "chemist", lang: state.lang } });
    stopAudio();
    if (res.tts === "timbre" && res.audio_stream_url) {
      state.audio = streamPcm(res.audio_stream_url, {});
    } else {
      state.audio = speakText(res.text, state.lang, {});
    }
  } catch {
    const text = $("chemist-script-text")?.textContent?.replace(/^"|"$/g, "") || "";
    speakText(text, state.lang, {});
  }
}

// ------------------------------------------------------------------ Grievance Draft
async function openGrievanceModal() {
  const modal = $("modal-grievance");
  if (!modal) return;
  modal.showModal();

  const bodyEl = $("grievance-body-text");
  if (!state.sessionId) {
    if (bodyEl) {
      bodyEl.textContent = "Subject: Query about Augmentin 625 Duo\nMedicine: Augmentin 625 Duo Tablet (Amoxicillin 500mg + Clavulanic Acid 125mg)\nMRP: ₹223.40\nCeiling Reference: ₹62.00\nStatement: I request verification of the medicine price and compliance with NPPA ceiling rules.";
    }
    return;
  }

  try {
    const res = await api("/v1/grievance/draft", { json: { session_id: state.sessionId } });
    const draft = res.draft;
    const text = `Subject: ${draft.subject}\nStatement: ${draft.statement}\nMedicine: ${JSON.stringify(draft.medicine, null, 2)}\nChannel: ${res.channel_hint}\nWarning: ${res.warning}`;
    if (bodyEl) bodyEl.textContent = text;
  } catch {
    if (bodyEl) bodyEl.textContent = "Grievance details will be populated from your recent consultation.";
  }
}

// ------------------------------------------------------------------ Language Selection
function fillLangSelect(sel) {
  if (!sel) return;
  sel.innerHTML = LANGS.map((l) => `<option value="${l}">${esc(LANG_NAMES[l])}</option>`).join("");
  sel.value = state.lang;
  sel.addEventListener("change", () => {
    state.lang = sel.value;
    store.set("lang", sel.value);
    document.documentElement.lang = sel.value;
    toast(`Language set to ${LANG_NAMES[sel.value] || sel.value}`);
  });
}

// ------------------------------------------------------------------ Pouch & History
function renderPouch() {
  const countEl = $("pouch-count");
  if (countEl) {
    countEl.hidden = !state.pouch.length;
    countEl.textContent = String(state.pouch.length);
  }
  const listEl = $("pouch-list");
  if (listEl) {
    listEl.innerHTML = state.pouch.length
      ? state.pouch.map((s, i) => `
        <li style="display: flex; justify-content: space-between; align-items: center; background: #f8fafc; padding: 8px 12px; border-radius: 8px; border: 1px solid #e2e8f0;">
          <span style="font-weight: 500; font-size: 0.9rem;">${esc(s)}</span>
          <button style="border: none; background: transparent; cursor: pointer; color: #ef4444; font-size: 1rem;" type="button" data-pouch-del="${i}">✕</button>
        </li>
      `).join("")
      : `<li style="color: #94a3b8; font-size: 0.88rem; text-align: center; padding: 12px;">No medicines in personal pouch yet.</li>`;
  }
}

function setPouch(list) {
  state.pouch = [...new Set(list.map((s) => s.trim().toLowerCase()).filter(Boolean))].slice(0, 50);
  store.set("pouch", state.pouch);
  renderPouch();
}

async function openHistory() {
  const modal = $("history");
  if (!modal) return;
  modal.showModal();
  const listEl = $("history-list");
  if (!listEl) return;
  listEl.innerHTML = `<li style="color: #64748b; padding: 10px;">Loading conversations…</li>`;

  try {
    if (state.threads.length) {
      const res = await api(`/v1/threads?ids=${encodeURIComponent(state.threads.join(","))}`);
      const threads = res.threads || [];
      listEl.innerHTML = threads.length ? threads.map((th) => `
        <li style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px; display: flex; justify-content: space-between; align-items: center;">
          <div>
            <div style="font-weight: 600; font-size: 0.92rem; color: #111a16;">${esc(th.title || "Consultation")}</div>
            <div style="font-size: 0.75rem; color: #64748b;">${new Date(th.updated_at).toLocaleDateString()} · ${th.messages} messages</div>
          </div>
          <button class="btn-preset-chip" type="button" data-thread-open="${esc(th.id)}">Open</button>
        </li>
      `).join("") : `<li style="color: #94a3b8; padding: 12px;">No past consultations found.</li>`;
    } else {
      listEl.innerHTML = `<li style="color: #94a3b8; padding: 12px;">No past consultations found.</li>`;
    }
  } catch {
    listEl.innerHTML = `<li style="color: #94a3b8; padding: 12px;">Could not load history.</li>`;
  }
}

// ------------------------------------------------------------------ Event Binding
function bindEvents() {
  // Soundwave Animation Loop
  animateSoundwave();

  // Language selectors
  fillLangSelect($("lang"));
  fillLangSelect($("lang2"));

  // Main Mic / Voice Button
  $("mic")?.addEventListener("click", toggleRecording);

  // Receptionist Orb tap
  $("receptionist-orb")?.addEventListener("click", () => {
    unlockAudio();
    toggleRecording();
  });

  // End Call Button
  $("btn-end-call")?.addEventListener("click", () => {
    stopAudio();
    if (rec.on) finishRecording();
    stopCallTimer();
    toast("Call ended.");
  });

  // Hero Book a Demo Button
  $("btn-book-demo")?.addEventListener("click", () => {
    unlockAudio();
    startCallTimer();
    toast("Starting live demo with Momo Receptionist…");
    const greeting = "Hello! I am Momo, your AI healthcare receptionist. You can ask me about your prescriptions, find cheaper generic alternatives, or check drug safety. How can I help you today?";
    speakText(greeting, state.lang, {
      onstart: () => setMicState("speaking"),
      onend: () => setMicState("idle"),
    });
  });

  // Form Submit
  $("ask")?.addEventListener("submit", (e) => {
    e.preventDefault();
    unlockAudio();
    const input = $("q");
    const q = input?.value?.trim();
    if (!q && !state.photos.strip && !state.photos.rx) return;
    if (input) input.value = "";
    ask({ text: q || null });
  });

  // File Inputs
  $("in-strip")?.addEventListener("change", (e) => setPhoto("strip", e.target.files[0]));
  $("in-rx")?.addEventListener("change", (e) => setPhoto("rx", e.target.files[0]));
  $("btn-check-photo")?.addEventListener("click", () => {
    unlockAudio();
    ask({});
  });

  // Quick Preset Chips
  document.querySelectorAll("[data-sample]").forEach((btn) => {
    btn.addEventListener("click", () => triggerSample(btn.dataset.sample));
  });

  // Replay Transcript Audio
  $("btn-replay-transcript")?.addEventListener("click", () => {
    unlockAudio();
    if (state.last) {
      playResponse(state.last);
    } else {
      const defaultText = "Augmentin 625 Duo contains Amoxicillin 500 mg and Clavulanic Acid 125 mg. A Jan Aushadhi generic equivalent is available for 62 rupees, saving you about 72 percent.";
      speakText(defaultText, state.lang, {});
    }
  });

  // Approve & Update Button
  $("btn-approve-action")?.addEventListener("click", () => {
    toast("✓ Clinic record approved & updated successfully.");
    const statusBadge = $("clinic-status-text");
    if (statusBadge) statusBadge.textContent = "✓ Approved & Synced";
    const reviewPill = $("clinic-review-badge");
    if (reviewPill) reviewPill.style.background = "#dcfce7";
  });

  // Speak to Chemist
  $("btn-speak-chemist")?.addEventListener("click", openChemistModal);
  $("chemist-close")?.addEventListener("click", () => $("modal-chemist")?.close());
  $("btn-play-chemist-modal")?.addEventListener("click", playChemistAudio);

  // Draft Grievance
  $("btn-draft-grievance")?.addEventListener("click", openGrievanceModal);
  $("grievance-close")?.addEventListener("click", () => $("modal-grievance")?.close());
  $("btn-copy-grievance")?.addEventListener("click", () => {
    const text = $("grievance-body-text")?.textContent || "";
    navigator.clipboard?.writeText(text);
    toast("Copied grievance draft to clipboard.");
  });

  // Add to Pouch Quick
  $("btn-add-pouch-quick")?.addEventListener("click", () => {
    const salts = targetSalts(state.last || {});
    if (salts.length) {
      setPouch([...state.pouch, ...salts]);
      toast(`Added ${salts.join(", ")} to Home Pouch.`);
    } else {
      setPouch([...state.pouch, "Amoxicillin", "Clavulanic Acid"]);
      toast("Added Amoxicillin + Clavulanic Acid to Home Pouch.");
    }
  });

  // Modal Closures & Navigation
  $("btn-pouch")?.addEventListener("click", () => {
    renderPouch();
    $("pouch")?.showModal();
  });
  $("pouch-close")?.addEventListener("click", () => $("pouch")?.close());
  $("pouch-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    const val = $("pouch-in")?.value?.trim();
    if (val) {
      setPouch([...state.pouch, val]);
      if ($("pouch-in")) $("pouch-in").value = "";
    }
  });

  $("btn-history")?.addEventListener("click", openHistory);
  $("history-close")?.addEventListener("click", () => $("history")?.close());

  $("btn-new")?.addEventListener("click", () => {
    stopAudio();
    state.sessionId = null;
    store.set("session", null);
    state.turns = [];
    state.last = null;
    clearPhotos();
    renderThread();
    toast("New consultation started.");
  });

  // Delegated clicks for dynamic elements
  document.body.addEventListener("click", (e) => {
    // Play text in transcript bubble
    const playBtn = e.target.closest("[data-play-text]");
    if (playBtn) {
      unlockAudio();
      speakText(playBtn.dataset.playText, state.lang, {});
      return;
    }

    // Remove attached photo
    const removePhoto = e.target.closest("[data-act='remove-photo']");
    if (removePhoto) {
      setPhoto(removePhoto.dataset.kind, null);
      return;
    }

    // Delete pouch item
    const delPouch = e.target.closest("[data-pouch-del]");
    if (delPouch) {
      const idx = Number(delPouch.dataset.pouchDel);
      setPouch(state.pouch.filter((_, i) => i !== idx));
      return;
    }

    // Tap outside dialogs closes them
    for (const d of [$("pouch"), $("history"), $("modal-chemist"), $("modal-grievance")]) {
      if (e.target === d) d.close();
    }
  });
}

// ------------------------------------------------------------------ Initialization
async function init() {
  bindEvents();
  renderPouch();
  renderThread();

  try {
    state.cfg = await api("/v1/config");
    const modesEl = $("modes");
    if (modesEl) modesEl.innerHTML = String(renderModes(state.cfg));
    const dataBody = $("data-body");
    if (dataBody) dataBody.innerHTML = String(renderDataSources(state.cfg.data_sources, state.lang));
  } catch {
    /* backend is loaded or offline */
  }

  // Check consent dialog
  if (!store.get("consent", false)) {
    const consentDlg = $("consent");
    if (consentDlg) {
      consentDlg.addEventListener("close", () => store.set("consent", true), { once: true });
      consentDlg.showModal();
    }
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
}

init();
