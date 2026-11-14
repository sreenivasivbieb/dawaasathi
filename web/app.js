import {
  Mic,
  Vad,
  encodeWav,
  streamPcm,
  speakText,
  unlockAudio,
} from "./audio.js";
import { LANGS, LANG_NAMES, t } from "./i18n.js";
import { renderResult, renderError, esc } from "./render.js";

const $ = (id) => document.getElementById(id);
const storage = {
  get(key, fallback) {
    try {
      return (
        JSON.parse(
          localStorage.getItem("momo." + key) ??
            localStorage.getItem("dawa." + key) ??
            "null",
        ) ?? fallback
      );
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem("momo." + key, JSON.stringify(value));
    } catch {
      /* Browser storage may be disabled. */
    }
  },
};
const savedLang = storage.get("lang", navigator.language.slice(0, 2));
const state = {
  lang: LANGS.includes(savedLang) ? savedLang : "en",
  session: storage.get("session", null),
  threads: storage.get("threads", []),
  photos: { strip: null, rx: null },
  urls: [],
  messages: [],
  last: null,
  cfg: null,
  busy: false,
  audio: null,
  speaking: false,
  voiceConversation: false,
  chemistOpen: false,
  fresh: false,
};
if (!Array.isArray(state.threads)) state.threads = [];
state.threads = state.threads
  .filter((id) => typeof id === "string" && /^s_[a-f0-9]{12,24}$/.test(id))
  .slice(0, 50);
const emptyResult = $("result").innerHTML;
let toastTimer;
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 5000);
}
async function api(path, { form, json, method } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method: method || (form || json ? "POST" : "GET"),
      headers: json ? { "Content-Type": "application/json" } : undefined,
      body: form || (json ? JSON.stringify(json) : undefined),
    });
  } catch {
    throw {
      code: "NETWORK",
      message: "Connection failed. Check your connection and retry.",
    };
  }
  let body;
  try {
    body = await response.json();
  } catch {
    throw {
      code: "INVALID_RESPONSE",
      message: "The service returned an unreadable response. Please retry.",
    };
  }
  if (!response.ok)
    throw (
      body.error || {
        code: "HTTP_" + response.status,
        message: "Request failed.",
      }
    );
  return body;
}
function showError(error) {
  $("result-status").hidden = false;
  $("request-error").hidden = false;
  $("request-error").innerHTML = String(renderError(error, state.lang));
  $("result-status").textContent = "Check failed · you can retry";
}
function clearError() {
  $("request-error").hidden = true;
  $("request-error").innerHTML = "";
}
function setBusy(busy) {
  state.busy = busy;
  document.querySelector(".results").setAttribute("aria-busy", String(busy));
  document
    .querySelectorAll(
      "#ask button, #result button, #btn-new, #btn-history, #lang, [data-thread-open], [data-thread-delete]",
    )
    .forEach((el) => {
      el.disabled = busy;
    });
  $("mic").disabled = busy || state.cfg?.stt !== "prisma";
  $("q").disabled = busy;
  if (busy) {
    $("result-status").hidden = false;
    $("result-status").textContent = "Checking…";
  }
  renderMic();
}
function remember(id) {
  state.session = id;
  storage.set("session", id);
  state.threads = [id, ...state.threads.filter((x) => x !== id)].slice(0, 50);
  storage.set("threads", state.threads);
}
function renderCurrent() {
  $("result").innerHTML = state.last
    ? String(
        renderResult(state.last, {
          lang: state.lang,
          sessionId: state.session,
          compact: true,
          chemistOpen: state.chemistOpen,
        }),
      )
    : emptyResult;
}
function renderMessages() {
  $("conversation").hidden = !state.messages.length;
  $("thread").innerHTML = state.messages.length
    ? state.messages
        .map(
          (m) =>
            `<div class="thread-row"><strong>${esc(m.role === "user" ? t(state.lang, "you_said") : t(state.lang, "reply"))}</strong><p>${esc(m.text || (m.attachments || []).map((a) => t(state.lang, a === "audio" ? "attached_audio" : "attached_photo")).join(" · "))}</p></div>`,
        )
        .join("")
    : `<p class="hint">${esc(t(state.lang, "history_empty"))}</p>`;
}
function receive(response, user) {
  remember(response.session_id);
  state.last = response;
  state.fresh = true;
  state.chemistOpen = false;
  if (user) state.messages.push({ role: "user", text: user });
  state.messages.push({
    role: "assistant",
    text: response.script?.text || response.card?.title || response.status,
  });
  renderCurrent();
  renderMessages();
  $("result-status").hidden = false;
  $("result-status").textContent =
    response.status === "needs_confirmation"
      ? "Choose a medicine below"
      : response.status === "needs_image"
        ? "Photo needed"
        : response.status === "error"
          ? "Could not complete check"
          : "Result received";
}
async function ask({ text = $("q").value.trim(), wav } = {}) {
  if (state.busy || rec.on) return;
  if (!text && !wav && !state.photos.strip && !state.photos.rx) {
    $("q").focus();
    return;
  }
  stopAudio();
  clearError();
  setBusy(true);
  const form = new FormData();
  form.set("lang", state.lang);
  form.set("channel", "web");
  if (state.session) form.set("session_id", state.session);
  if (text) form.set("text", text);
  if (wav)
    form.set("audio", new Blob([wav], { type: "audio/wav" }), "speech.wav");
  for (const [kind, file] of Object.entries(state.photos))
    if (file) form.set(kind, file, file.name);
  const label =
    text ||
    (wav
      ? t(state.lang, "attached_audio")
      : Object.entries(state.photos)
          .filter(([, f]) => f)
          .map(([kind]) => t(state.lang, kind + "_photo"))
          .join(" + "));
  try {
    const response = await api("/v1/query", { form });
    receive(response, label);
    state.voiceConversation = !!wav;
    if (wav && response.status !== "error") playResponse(response);
    // Preserve typed input and attachments when the pipeline itself reports a failure.
    if (response.status !== "error") {
      $("q").value = "";
      clearPhotos();
    }
  } catch (error) {
    showError(error);
  } finally {
    setBusy(false);
  }
}
async function confirm(candidate, none = false) {
  if (state.busy || !state.session) return;
  stopAudio();
  clearError();
  setBusy(true);
  try {
    const choice = state.last?.candidates?.find(
      (c) => c.candidate_id === candidate,
    )?.label;
    receive(
      await api("/v1/confirm", {
        json: {
          session_id: state.session,
          candidate_id: candidate || null,
          none,
          lang: state.lang,
        },
      }),
      choice || t(state.lang, "none_of_these"),
    );
    if (state.voiceConversation && state.last?.status !== "error")
      playResponse(state.last);
  } catch (error) {
    showError(error);
  } finally {
    setBusy(false);
  }
}
function stopAudio() {
  try {
    state.audio?.stop();
  } catch {
    /* Already stopped. */
  }
  state.audio = null;
  state.speaking = false;
  renderMic();
  if (state.session)
    api("/v1/interrupt", { json: { session_id: state.session } }).catch(
      () => {},
    );
}
function playResponse(response) {
  stopAudio();
  unlockAudio();
  const onstart = () => {
    state.speaking = true;
    renderMic();
  };
  const onend = () => {
    state.speaking = false;
    renderMic();
  };
  const readInBrowser = () => {
    state.audio = speakText(
      response.script?.speech_text || response.script?.text,
      response.lang || state.lang,
      { onstart, onend },
    );
  };
  if (response?.tts === "timbre" && response.audio_stream_url) {
    state.audio = streamPcm(response.audio_stream_url, {
      onstart,
      onended: onend,
      onerror: () => {
        onend();
        toast(
          "Gnani audio unavailable. Reading the answer using your browser.",
        );
        readInBrowser();
      },
    });
  } else if (response?.script?.text) readInBrowser();
}
async function speakChemist() {
  if (!state.last?.script?.chemist_text || state.busy) return;
  unlockAudio();
  stopAudio();
  const lang = state.last.lang || state.lang;
  const fallback = () =>
    playResponse({ script: { text: state.last.script.chemist_text }, lang });
  if (!state.fresh) {
    fallback();
    return;
  }
  try {
    const response = await api("/v1/speak", {
      json: { session_id: state.session, which: "chemist", lang },
    });
    playResponse({ ...response, script: { text: response.text }, lang });
  } catch {
    fallback();
  }
}
const mic = new Mic();
const rec = {
  on: false,
  starting: false,
  epoch: 0,
  frames: [],
  heard: false,
  ms: 0,
  timer: null,
};
function renderMic() {
  const connected = state.cfg?.stt === "prisma";
  const phase = rec.on
    ? "recording"
    : rec.starting
      ? "starting"
      : state.speaking
        ? "speaking"
        : state.busy
          ? "thinking"
          : "idle";
  $("voice-panel").dataset.state = phase;
  $("mic").hidden = false;
  $("mic").disabled = !connected || state.busy || rec.starting;
  $("mic").dataset.state = phase;
  $("mic").setAttribute("aria-pressed", String(rec.on));
  $("mic").setAttribute(
    "aria-label",
    t(state.lang, rec.on ? "mic_aria_stop" : "mic_aria_start"),
  );
  $("voice-title").textContent = t(state.lang, "tagline");
  $("mic-label").textContent = t(
    state.lang,
    rec.on
      ? "mic_listening"
      : state.speaking
        ? "mic_speaking"
        : state.busy
          ? "mic_thinking"
          : connected
            ? "mic_idle"
            : "err_STT_NOT_CONFIGURED",
  );
  $("voice-badge").textContent = LANG_NAMES[state.lang];
  $("voice-badge").classList.toggle("connected", connected);
  $("voice-setup").hidden = connected;
  $("mic-status").hidden = false;
  $("mic-status").textContent = t(
    state.lang,
    rec.on
      ? "mic_listening"
      : state.speaking
        ? "mic_speaking"
        : connected
          ? "mic_hint"
          : "err_STT_NOT_CONFIGURED",
  );
  $("voice-stop").textContent = t(state.lang, "stop");
  $("lang").disabled = state.busy || rec.on || rec.starting;
  $("voice-stop").hidden = !rec.on && !state.speaking;
  $("send").disabled = state.busy || rec.on || rec.starting;
}
async function toggleRecording() {
  if (rec.on) {
    finishRecording();
    return;
  }
  if (rec.starting || state.busy || state.cfg?.stt !== "prisma") return;
  stopAudio();
  unlockAudio();
  const epoch = ++rec.epoch;
  rec.starting = true;
  renderMic();
  try {
    await mic.start(16000);
    if (epoch !== rec.epoch || document.hidden) {
      mic.stop();
      return;
    }
    Object.assign(rec, { on: true, frames: [], heard: false, ms: 0 });
    const vad = new Vad();
    mic.onframe = (pcm, rms, ms) => {
      if (!rec.on) return;
      rec.frames.push(pcm);
      rec.ms += ms;
      const event = vad.push(rms, ms);
      if (event === "start") rec.heard = true;
      if ((event === "end" && rec.heard) || rec.ms >= 20000) finishRecording();
    };
    rec.timer = setTimeout(finishRecording, 22000);
    renderMic();
  } catch (error) {
    toast(
      t(
        state.lang,
        error.name === "NotAllowedError" ? "mic_denied" : "mic_unsupported",
      ),
    );
  } finally {
    if (epoch === rec.epoch) rec.starting = false;
    renderMic();
  }
}
function cancelRecording() {
  rec.epoch++;
  rec.starting = false;
  clearTimeout(rec.timer);
  rec.on = false;
  mic.onframe = null;
  mic.stop();
  rec.frames = [];
  renderMic();
}
function finishRecording() {
  if (!rec.on) return;
  const wav =
    rec.heard && rec.ms >= 400 ? encodeWav(rec.frames, mic.targetRate) : null;
  cancelRecording();
  if (wav) ask({ text: "", wav });
  else toast(t(state.lang, "no_speech"));
}
function setPhoto(kind, file) {
  if (
    file &&
    (![
      "image/jpeg",
      "image/png",
      "image/webp",
      "image/heic",
      "image/heif",
    ].includes(file.type) ||
      file.size > (state.cfg?.max_image_bytes || 10 * 1024 * 1024))
  ) {
    toast(
      file.size > (state.cfg?.max_image_bytes || 10 * 1024 * 1024)
        ? t(state.lang, "err_TOO_LARGE")
        : t(state.lang, "err_UNSUPPORTED_MEDIA"),
    );
    $("in-" + kind).value = "";
    return;
  }
  state.photos[kind] = file || null;
  if (!file) $("in-" + kind).value = "";
  renderThumbs();
}
function clearPhotos() {
  for (const kind of ["strip", "rx"]) {
    state.photos[kind] = null;
    $("in-" + kind).value = "";
  }
  renderThumbs();
}
function renderThumbs() {
  state.urls.forEach((url) => URL.revokeObjectURL(url));
  state.urls = [];
  $("thumbs").innerHTML = Object.entries(state.photos)
    .filter(([, file]) => file)
    .map(([kind, file]) => {
      const url = URL.createObjectURL(file);
      state.urls.push(url);
      return `<div class="thumb"><img src="${url}" alt="${esc(t(state.lang, kind + "_photo"))}"><span>${esc(file.name)}</span><button type="button" data-act="remove-photo" data-kind="${kind}" aria-label="${esc(t(state.lang, "remove") + " " + t(state.lang, kind + "_photo"))}">×</button></div>`;
    })
    .join("");
}
async function openHistory(refresh = false) {
  if ((!refresh && state.busy) || rec.on) return;
  if (!$("history").open) $("history").showModal();
  $("history-list").innerHTML = "<li>Loading…</li>";
  try {
    const { threads } = await api(
      "/v1/threads?ids=" + encodeURIComponent(state.threads.join(",")),
    );
    $("history-list").innerHTML = threads.length
      ? threads
          .map(
            (thread) =>
              `<li><span>${esc(thread.title || t(state.lang, "attached_photo"))}<small>${esc(new Date(thread.updated_at).toLocaleString())} · ${esc(t(state.lang, "history_messages", { n: thread.messages }))}</small></span><button type="button" data-thread-open="${esc(thread.id)}">Open</button><button type="button" data-thread-delete="${esc(thread.id)}">${esc(t(state.lang, "history_delete"))}</button></li>`,
          )
          .join("")
      : `<li>${esc(t(state.lang, "history_empty"))}</li>`;
  } catch (error) {
    $("history-list").innerHTML = `<li>${esc(error.message)}</li>`;
  }
}
async function loadThread(id, close = true) {
  if (state.busy) return;
  setBusy(true);
  clearError();
  stopAudio();
  try {
    const thread = await api("/v1/threads/" + encodeURIComponent(id));
    state.session = id;
    storage.set("session", id);
    state.messages = thread.messages;
    state.last =
      [...thread.messages].reverse().find((m) => m.response)?.response || null;
    // Audio jobs expire independently of persistent history.
    if (state.last)
      state.last = {
        ...state.last,
        audio_url: null,
        audio_stream_url: null,
        tts: "none",
      };
    state.fresh = false;
    state.chemistOpen = false;
    clearPhotos();
    $("q").value = "";
    renderCurrent();
    renderMessages();
    $("result-status").textContent =
      "Saved result · ask again for a fresh check";
    if (close) $("history").close();
  } catch (error) {
    if (error.code === "THREAD_NOT_FOUND") {
      state.threads = state.threads.filter((x) => x !== id);
      storage.set("threads", state.threads);
      if (state.session === id) {
        state.session = null;
        storage.set("session", null);
      }
    }
    showError(error);
  } finally {
    setBusy(false);
  }
}
async function deleteThread(id) {
  if (state.busy) return;
  setBusy(true);
  try {
    await api("/v1/threads/" + encodeURIComponent(id), { method: "DELETE" });
    state.threads = state.threads.filter((x) => x !== id);
    storage.set("threads", state.threads);
    if (state.session === id) reset();
    toast(t(state.lang, "history_deleted"));
    await openHistory(true);
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}
function reset() {
  stopAudio();
  cancelRecording();
  state.session = null;
  storage.set("session", null);
  state.messages = [];
  state.last = null;
  state.fresh = false;
  state.voiceConversation = false;
  state.chemistOpen = false;
  clearPhotos();
  clearError();
  $("q").value = "";
  renderCurrent();
  renderMessages();
  $("result-status").textContent = "";
  $("result-status").hidden = true;
}
function translate() {
  document.documentElement.lang = state.lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(state.lang, el.dataset.i18n);
  });
  $("q").placeholder = t(state.lang, "type_placeholder");
  renderMic();
  renderCurrent();
  renderMessages();
  renderThumbs();
}
async function connect() {
  try {
    state.cfg = await api("/v1/config");
    const synthetic = Object.values(state.cfg.data_sources || {}).some(
      (source) =>
        /synthetic|fixture|demo|seed_dev/i.test(JSON.stringify(source)),
    );
    $("connection").textContent = synthetic
      ? "Demo data · Medicine details and prices are for demonstration only."
      : "";
    $("connection").hidden = !synthetic;
    $("connection").classList.toggle("warning", synthetic);
    $("retry-connection").hidden = true;
  } catch {
    state.cfg = null;
    $("connection").textContent = "Unable to connect. Please retry.";
    $("connection").hidden = false;
    $("connection").classList.add("warning");
    $("retry-connection").hidden = false;
  }
  $("mic").disabled = state.busy || state.cfg?.stt !== "prisma";
  renderMic();
}
$("lang").innerHTML = LANGS.map(
  (lang) => `<option value="${lang}">${LANG_NAMES[lang]}</option>`,
).join("");
$("lang").value = state.lang;
$("lang").addEventListener("change", () => {
  stopAudio();
  state.lang = $("lang").value;
  storage.set("lang", state.lang);
  translate();
});
$("ask").addEventListener("submit", (event) => {
  event.preventDefault();
  ask();
});
$("mic").addEventListener("click", toggleRecording);
$("voice-stop").addEventListener("click", () => {
  stopAudio();
  cancelRecording();
});
$("refresh-voice").addEventListener("click", connect);
for (const kind of ["strip", "rx"])
  $("in-" + kind).addEventListener("change", (event) =>
    setPhoto(kind, event.target.files[0]),
  );
$("btn-new").addEventListener("click", () => {
  if (!state.busy) reset();
});
$("btn-history").addEventListener("click", () => openHistory());
$("retry-connection").addEventListener("click", connect);
document.body.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;
  if (button.dataset.close) {
    $(button.dataset.close).close();
    return;
  }
  if (button.dataset.threadOpen) {
    loadThread(button.dataset.threadOpen);
    return;
  }
  if (button.dataset.threadDelete) {
    deleteThread(button.dataset.threadDelete);
    return;
  }
  if (state.busy) return;
  switch (button.dataset.act) {
    case "pick":
      $("in-" + button.dataset.kind)?.click();
      break;
    case "remove-photo":
      setPhoto(button.dataset.kind, null);
      break;
    case "confirm":
      confirm(button.dataset.id);
      break;
    case "confirm-none":
      confirm(null, true);
      break;
    case "replay":
      playResponse(state.last);
      break;
    case "stop-audio":
      stopAudio();
      break;
    case "chemist-toggle":
      state.chemistOpen = !state.chemistOpen;
      renderCurrent();
      break;
    case "chemist-play":
      speakChemist();
      break;
  }
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden && rec.on) cancelRecording();
});
window.addEventListener("pagehide", () => {
  cancelRecording();
  stopAudio();
  state.urls.forEach((url) => URL.revokeObjectURL(url));
});
translate();
await connect();
if (state.session) await loadThread(state.session, false);
if ("serviceWorker" in navigator)
  navigator.serviceWorker.register("/sw.js").catch(() => {});
