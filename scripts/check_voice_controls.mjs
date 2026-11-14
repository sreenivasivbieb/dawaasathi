// Voice-control integration check: simulated microphone and configured-state response;
// actual local backend queries. This does not test live Gnani recognition or audible playback.
const { JSDOM } = await import(process.env.JSDOM_MODULE || "jsdom");
import { readFile, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import assert from "node:assert/strict";
const root = process.cwd(),
  base = process.env.DAWA_CHECK_URL || "http://127.0.0.1:8002";
const dom = new JSDOM(await readFile(root + "/web/index.html", "utf8"), {
  url: base,
  pretendToBeVisual: true,
});
for (const key of ["window", "document", "navigator", "localStorage"])
  Object.defineProperty(globalThis, key, {
    value: dom.window[key],
    configurable: true,
  });
const $ = (selector) => document.querySelector(selector);
const wait = async (condition) => {
  for (let i = 0; i < 800; i++) {
    if (condition()) return;
    await new Promise((r) => setTimeout(r, 15));
  }
  throw Error("Timed out");
};
const nativeFetch = globalThis.fetch;
let connected = false,
  micInstance,
  audioRequests = [],
  spoken = [],
  threads = new Set();
globalThis.AudioContext = class {
  state = "running";
};
globalThis.SpeechSynthesisUtterance = class {
  constructor(text) {
    this.text = text;
  }
};
globalThis.speechSynthesis = {
  cancel() {},
  getVoices() {
    return [];
  },
  speak(utterance) {
    spoken.push(utterance.lang);
    utterance.onstart?.();
    queueMicrotask(() => utterance.onend?.());
  },
};
const { Mic } = await import(pathToFileURL(root + "/web/audio.js").href);
Mic.prototype.start = async function (rate) {
  micInstance = this;
  this.targetRate = rate;
};
Mic.prototype.stop = function () {};
globalThis.fetch = async (path, options) => {
  if (path === "/v1/config") {
    const r = await nativeFetch(base + path);
    const cfg = await r.json();
    if (connected) cfg.stt = "prisma";
    return Response.json(cfg);
  }
  if (path === "/v1/query" && options.body.has("audio")) {
    audioRequests.push({
      lang: options.body.get("lang"),
      file: options.body.get("audio"),
    });
    // Controlled transcript: exercise the real pipeline and browser playback without claiming STT.
    options.body.set("text", "Dolo 650 price and generic");
  }
  const response = await nativeFetch(new URL(path, base), options);
  if (path === "/v1/query") {
    const result = await response.clone().json();
    if (result.session_id) threads.add(result.session_id);
  }
  return response;
};
await import(pathToFileURL(root + "/web/app.js").href);
assert.equal($("#mic").hidden, false);
assert.equal($("#mic").disabled, true);
assert.equal($("#voice-setup").hidden, false);
connected = true;
$("#refresh-voice").click();
await wait(() => !$("#mic").disabled);
const { LANGS, LANG_NAMES, t } = await import(
  pathToFileURL(root + "/web/i18n.js").href
);
const checks = [];
for (const lang of LANGS) {
  $("#lang").value = lang;
  $("#lang").dispatchEvent(new dom.window.Event("change"));
  assert.equal($("#voice-badge").textContent, LANG_NAMES[lang]);
  assert.equal($("#mic").getAttribute("aria-label"), t(lang, "mic_aria_start"));
  assert.equal($("#mic-label").textContent, t(lang, "mic_idle"));
  const previous = audioRequests.length;
  $("#mic").click();
  await wait(() => $("#mic").dataset.state === "recording");
  assert.equal($("#lang").disabled, true);
  assert.equal($("#mic").getAttribute("aria-label"), t(lang, "mic_aria_stop"));
  for (let i = 0; i < 4; i++)
    micInstance.onframe(new Int16Array(800), 0.001, 50);
  for (let i = 0; i < 8; i++)
    micInstance.onframe(new Int16Array(800).fill(1000), 0.06, 50);
  $("#mic").click();
  await wait(
    () =>
      $(".results").getAttribute("aria-busy") === "false" &&
      audioRequests.length > previous,
  );
  assert.equal(audioRequests.at(-1).lang, lang);
  assert.equal(audioRequests.at(-1).file.type, "audio/wav");
  assert.ok(audioRequests.at(-1).file.size > 44);
  await wait(() => spoken.includes(lang + "-IN"));
  assert.equal($("#result [lang]").getAttribute("lang"), lang);
  checks.push({
    language: lang,
    controls: true,
    wav_request: true,
    reply_language: true,
    playback_language: true,
  });
  console.log("PASS voice controls and language routing:", lang);
}
const count = audioRequests.length;
$("#mic").click();
await wait(() => $("#mic").dataset.state === "recording");
$("#voice-stop").click();
assert.equal($("#mic").dataset.state, "idle");
assert.equal(audioRequests.length, count);
for (const id of threads)
  await nativeFetch(base + "/v1/threads/" + id, { method: "DELETE" });
const report = {
  checks,
  microphone: "simulated",
  transcript: "controlled text sent with audio",
  gnani_live_verified: false,
  audible_playback_verified: false,
  cancel_without_submission: true,
};
if (process.env.DAWA_VOICE_REPORT)
  await writeFile(
    process.env.DAWA_VOICE_REPORT,
    JSON.stringify(report, null, 2) + "\n",
  );
console.log(
  "11/11 voice-language flows passed; live Gnani and physical capture not verified.",
);
dom.window.close();
process.exit(0);
