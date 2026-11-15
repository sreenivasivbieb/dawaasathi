// Audio pipeline for Dawaa Saathi.
//
//  Capture:  getUserMedia (AEC/NS/AGC on) -> MediaStreamSource -> AudioWorklet ("dawa-recorder",
//            posts ~1024-sample Float32 batches at the device rate, usually 48 kHz)
//            -> main thread: RMS for the VAD + Downsampler (box-filter decimation) -> Int16 PCM
//            at 16 kHz (8 kHz in phone mode).
//  VAD:      energy threshold over an adaptive noise floor; separate, stricter instance for
//            barge-in while the reply is playing.
//  Upload:   Int16 frames -> mono 16-bit WAV (encodeWav).
//  Playback: fetch(...).body.getReader() streaming raw s16le PCM -> PcmDecoder (keeps the odd
//            byte across chunk boundaries) -> StreamPlayer (AudioBufferSourceNodes scheduled
//            back-to-back on one AudioContext).
//  Fallback: speechSynthesis when the server's TTS is "mock" (no Gnani key).
//
// Downsampler / PcmDecoder / Vad / PreRoll / encodeWav have no DOM dependencies (unit-testable).

import { SPEECH_LANG } from "./i18n.js";

// ------------------------------------------------------------------ shared AudioContext
let ctx = null;
export function audioContext() {
  if (!ctx || ctx.state === "closed") {
    const AC = globalThis.AudioContext || globalThis.webkitAudioContext;
    if (!AC) throw Object.assign(new Error("Web Audio is unavailable"), { name: "NotSupportedError" });
    ctx = new AC({ latencyHint: "interactive" });
  }
  return ctx;
}
/** Call synchronously from a user gesture (iOS/Chrome autoplay rules), before any await. */
export function unlockAudio() {
  const c = audioContext();
  if (c.state !== "running") c.resume().catch(() => {});
  return c;
}

// ------------------------------------------------------------------ resampling
/**
 * Streaming Float32 -> Int16 resampler. For downsampling (the normal case: 48k -> 16k/8k) each
 * output sample is the mean of the input samples it covers (a box low-pass filter, enough to keep
 * aliasing out of the speech band). Leftover input is carried to the next call, so frame
 * boundaries never drop or duplicate samples. Upsampling (rare) uses linear interpolation.
 */
export class Downsampler {
  constructor(inRate, outRate) {
    this.inRate = inRate;
    this.outRate = outRate;
    this.ratio = inRate / outRate;
    this.tail = new Float32Array(0);
    this.pos = 0; // fractional read position into (tail + next input)
  }
  process(input) {
    let data = input;
    if (this.tail.length) {
      data = new Float32Array(this.tail.length + input.length);
      data.set(this.tail);
      data.set(input, this.tail.length);
    }
    const ratio = this.ratio;
    const out = new Int16Array(Math.max(0, Math.ceil((data.length - this.pos) / ratio) + 1));
    let n = 0;
    let pos = this.pos;
    if (ratio >= 1) {
      while (pos + ratio <= data.length) {
        const a = Math.floor(pos);
        const b = Math.max(a + 1, Math.floor(pos + ratio));
        let sum = 0;
        for (let i = a; i < b; i++) sum += data[i];
        out[n++] = toInt16(sum / (b - a));
        pos += ratio;
      }
    } else {
      while (pos + 1 < data.length) {
        const i = Math.floor(pos);
        const f = pos - i;
        out[n++] = toInt16(data[i] * (1 - f) + data[i + 1] * f);
        pos += ratio;
      }
    }
    const consumed = Math.min(Math.floor(pos), data.length);
    this.tail = data.slice(consumed);
    this.pos = pos - consumed;
    return out.subarray(0, n);
  }
}
function toInt16(v) {
  const c = v < -1 ? -1 : v > 1 ? 1 : v;
  return c < 0 ? Math.round(c * 0x8000) : Math.round(c * 0x7fff);
}

// ------------------------------------------------------------------ VAD
/**
 * Energy VAD with an adaptive noise floor.
 *  - First `warmMs`: no decisions, floor = quietest frame seen (calibrates to the room).
 *  - Then: floor falls fast toward quieter frames and rises slowly (very slowly while "loud"),
 *    so speech doesn't drag it up but a fan switching on eventually does.
 *  - Speech starts after `startMs` of frames above max(floor * factor, minAbs);
 *    ends after `hangMs` below 60% of that threshold (hysteresis).
 * push() returns "start" | "end" | null.
 */
export class Vad {
  constructor({ factor = 3, minAbs = 0.012, startMs = 90, hangMs = 900, warmMs = 200 } = {}) {
    Object.assign(this, { factor, minAbs, startMs, hangMs, warmMs });
    this.reset();
  }
  reset() {
    this.floor = 1;
    this.ageMs = 0;
    this.speaking = false;
    this.loudMs = 0;
    this.silentMs = 0;
  }
  threshold() {
    return Math.max(this.floor * this.factor, this.minAbs);
  }
  push(rms, ms) {
    this.ageMs += ms;
    if (this.ageMs <= this.warmMs) {
      this.floor = Math.max(0.0005, Math.min(this.floor, rms, 0.004));
      return null;
    }
    const thr = this.threshold();
    const loud = rms > thr;
    const a = rms < this.floor ? 0.15 : loud ? 0.002 : 0.03;
    this.floor = Math.min(0.2, Math.max(0.0005, this.floor + (rms - this.floor) * a));
    if (!this.speaking) {
      this.loudMs = loud ? this.loudMs + ms : 0;
      if (this.loudMs >= this.startMs) {
        this.speaking = true;
        this.silentMs = 0;
        return "start";
      }
    } else {
      this.silentMs = rms > thr * 0.6 ? 0 : this.silentMs + ms;
      if (this.silentMs >= this.hangMs) {
        this.speaking = false;
        this.loudMs = 0;
        return "end";
      }
    }
    return null;
  }
}

/** Ring buffer of the most recent `ms` of Int16 frames (barge-in pre-roll). */
export class PreRoll {
  constructor(ms) { this.ms = ms; this.frames = []; this.total = 0; }
  push(pcm, ms) {
    this.frames.push([pcm, ms]);
    this.total += ms;
    while (this.frames.length > 1 && this.total - this.frames[0][1] >= this.ms) this.total -= this.frames.shift()[1];
  }
  take() {
    const f = this.frames.map((x) => x[0]);
    this.frames = [];
    this.total = 0;
    return f;
  }
}

// ------------------------------------------------------------------ WAV
/** Int16 frames -> ArrayBuffer holding a mono 16-bit PCM WAV file. */
export function encodeWav(chunks, sampleRate) {
  const n = chunks.reduce((a, c) => a + c.length, 0);
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE");
  str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, sampleRate, true); v.setUint32(28, sampleRate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, n * 2, true);
  let o = 44;
  for (const c of chunks) for (let i = 0; i < c.length; i++, o += 2) v.setInt16(o, c[i], true);
  return buf;
}

// ------------------------------------------------------------------ PCM decoding
/**
 * Raw s16le bytes -> Float32 samples. Network chunks can end in the middle of a sample, so an odd
 * trailing byte is kept and prepended to the next chunk.
 */
export class PcmDecoder {
  constructor() { this.carry = -1; }
  push(u8) {
    let bytes = u8;
    if (this.carry >= 0) {
      bytes = new Uint8Array(u8.length + 1);
      bytes[0] = this.carry;
      bytes.set(u8, 1);
      this.carry = -1;
    }
    const even = bytes.length & ~1;
    if (even !== bytes.length) this.carry = bytes[bytes.length - 1];
    const n = even >> 1;
    const out = new Float32Array(n);
    const dv = new DataView(bytes.buffer, bytes.byteOffset, even);
    for (let i = 0; i < n; i++) out[i] = dv.getInt16(i * 2, true) / 32768;
    return out;
  }
}

function concatF32(parts, total) {
  if (parts.length === 1) return parts[0];
  const out = new Float32Array(total);
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

/** AudioBuffer at `sr`; if the browser refuses that rate (old Safari < 22.05 kHz) resample to the context rate. */
function makeBuffer(c, data, sr) {
  try {
    const b = c.createBuffer(1, data.length, sr);
    b.copyToChannel ? b.copyToChannel(data, 0) : b.getChannelData(0).set(data);
    return b;
  } catch {
    const ratio = sr / c.sampleRate;
    const n = Math.max(1, Math.floor(data.length / ratio));
    const b = c.createBuffer(1, n, c.sampleRate);
    const ch = b.getChannelData(0);
    for (let i = 0; i < n; i++) {
      const p = i * ratio, j = Math.floor(p), f = p - j;
      ch[i] = (data[j] || 0) * (1 - f) + (data[j + 1] ?? data[j] ?? 0) * f;
    }
    return b;
  }
}

// ------------------------------------------------------------------ streamed playback
/**
 * Schedules Float32 chunks gap-free on the shared AudioContext. Tiny network chunks are coalesced
 * (>= 60 ms) once enough audio is queued ahead; the first chunk starts after a 100 ms cushion.
 * If the network stalls and we fall behind, playback resumes slightly in the future rather than
 * scheduling in the past.
 */
export class StreamPlayer {
  constructor(sampleRate, { onstart, onended } = {}) {
    this.ctx = audioContext();
    this.sr = sampleRate;
    this.onstart = onstart;
    this.onended = onended;
    this.next = 0;
    this.sources = new Set();
    this.pending = [];
    this.pendingLen = 0;
    this.all = []; // every chunk, kept for replay
    this.allLen = 0;
    this.started = false;
    this.done = false;
    this.stopped = false;
    this.ended = false;
    this.minChunk = Math.round(sampleRate * 0.06);
    this.gain = this.ctx.createGain();
    this.gain.connect(this.ctx.destination);
  }
  push(f32) {
    if (this.stopped || !f32 || !f32.length) return;
    if (!this.started) { this.started = true; this.onstart?.(); }
    this.all.push(f32);
    this.allLen += f32.length;
    this.pending.push(f32);
    this.pendingLen += f32.length;
    const ahead = this.next - this.ctx.currentTime;
    if (ahead < 0.15 || this.pendingLen >= this.minChunk) this._flush();
  }
  end() {
    this.done = true;
    this._flush();
    this._maybeEnded();
  }
  _flush() {
    if (!this.pendingLen || this.stopped) return;
    const data = concatF32(this.pending, this.pendingLen);
    this.pending = [];
    this.pendingLen = 0;
    const buf = makeBuffer(this.ctx, data, this.sr);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.gain);
    const now = this.ctx.currentTime;
    if (this.next < now + 0.02) this.next = now + (this.sources.size ? 0.03 : 0.1);
    src.start(this.next);
    this.next += buf.duration;
    this.sources.add(src);
    src.onended = () => { this.sources.delete(src); this._maybeEnded(); };
  }
  _maybeEnded() {
    if (this.done && !this.stopped && !this.ended && this.sources.size === 0 && !this.pendingLen) {
      this.ended = true;
      this.onended?.();
    }
  }
  /** Immediate stop (barge-in). Does not fire onended. */
  stop() {
    if (this.stopped) return;
    this.stopped = true;
    for (const s of this.sources) { try { s.onended = null; s.stop(); } catch { /* not started */ } }
    this.sources.clear();
    this.pending = [];
    this.pendingLen = 0;
    try { this.gain.disconnect(); } catch { /* already */ }
  }
  get active() { return !this.stopped && !this.ended; }
  /** All audio received so far as one buffer (for replay). */
  samples() { return concatF32(this.all, this.allLen); }
}

/** Play a complete Float32 buffer (filler line, replay from memory). */
export function playSamples(f32, sr, handlers) {
  const p = new StreamPlayer(sr, handlers);
  p.push(f32);
  p.end();
  return p;
}

/**
 * Stream raw PCM (s16le mono, rate in X-Sample-Rate) from `url` and play it as it arrives.
 * Returns { stop(), done: Promise<StreamPlayer|null>, player }.
 */
export function streamPcm(url, { onstart, onended, onerror } = {}) {
  const ac = new AbortController();
  const ctl = { player: null, stopped: false };
  ctl.stop = () => { ctl.stopped = true; ac.abort(); ctl.player?.stop(); };
  ctl.done = (async () => {
    const res = await fetch(url, { signal: ac.signal, cache: "no-store" });
    if (!res.ok || !res.body) throw new Error(`audio HTTP ${res.status}`);
    const sr = parseInt(res.headers.get("X-Sample-Rate") || "24000", 10) || 24000;
    const player = (ctl.player = new StreamPlayer(sr, { onstart, onended }));
    const dec = new PcmDecoder();
    const reader = res.body.getReader();
    let received = 0;
    for (;;) {
      const { value, done } = await reader.read();
      if (done || ctl.stopped) break;
      received += value.byteLength;
      player.push(dec.push(value));
    }
    if (!ctl.stopped && received < 2) throw new Error("Empty speech audio");
    player.end();
    return player;
  })().catch((err) => {
    if (!ctl.stopped && err.name !== "AbortError") onerror?.(err);
    return null;
  });
  return ctl;
}

/** Fetch a whole PCM response (filler line). Returns {sr, samples, live} or null when silent/mock. */
export async function fetchPcm(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const live = (res.headers.get("X-TTS") || "") !== "mock";
  const sr = parseInt(res.headers.get("X-Sample-Rate") || "24000", 10) || 24000;
  const bytes = new Uint8Array(await res.arrayBuffer());
  if (!live) return null; // mock TTS = silence: nothing worth playing
  return { sr, samples: new PcmDecoder().push(bytes), live };
}

// ------------------------------------------------------------------ microphone
let workletReady = null;

/**
 * Microphone capture. onframe(pcmInt16, rms, ms) is called for every ~21 ms batch.
 * The AudioWorklet output goes into a zero-gain node so the graph is pulled but nothing is heard.
 */
export class Mic {
  constructor() {
    this.stream = null;
    this.targetRate = 16000;
    this.onframe = null;
    this.ds = null;
  }
  get open() { return !!this.stream; }
  async start(targetRate) {
    this.setTargetRate(targetRate);
    if (this.stream) return;
    this._starting ||= this._open().finally(() => { this._starting = null; });
    return this._starting;
  }
  async _open() {
    const fail = (name, message) => Object.assign(new Error(message), { name });
    if (globalThis.isSecureContext === false) {
      throw fail("InsecureContextError", "Microphone access requires localhost or HTTPS.");
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      throw fail("NotSupportedError", "This browser does not expose microphone capture.");
    }
    const c = audioContext();
    if (c.state === "suspended") await c.resume();
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch (error) {
      if (error.name !== "OverconstrainedError") throw error;
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    }
    this.stream = stream;
    this.ds = new Downsampler(c.sampleRate, this.targetRate);
    try {
      this.src = c.createMediaStreamSource(stream);
      if (c.audioWorklet?.addModule && globalThis.AudioWorkletNode) {
        try {
          workletReady ||= c.audioWorklet.addModule(new URL("./recorder-worklet.js", import.meta.url).href);
          await workletReady;
          this.node = new AudioWorkletNode(c, "dawa-recorder", {
            numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], processorOptions: { batch: 1024 },
          });
          this.node.port.onmessage = (e) => this._batch(e.data);
          this.mode = "worklet";
        } catch {
          workletReady = null;
        }
      }
      // Compatibility path for WebViews/browsers that lack or reject AudioWorklet.
      if (!this.node) {
        if (typeof c.createScriptProcessor !== "function") {
          throw fail("CaptureUnavailableError", "Neither audio capture processor is available.");
        }
        this.node = c.createScriptProcessor(2048, 1, 1);
        this.node.onaudioprocess = (e) => this._batch(e.inputBuffer.getChannelData(0));
        this.mode = "compatibility";
      }
      this.sink = c.createGain();
      this.sink.gain.value = 0;
      this.src.connect(this.node);
      this.node.connect(this.sink);
      this.sink.connect(c.destination);
    } catch (error) {
      this.stop();
      throw error;
    }
  }
  setTargetRate(rate) {
    if (rate === this.targetRate && this.ds) return;
    this.targetRate = rate;
    if (ctx) this.ds = new Downsampler(ctx.sampleRate, rate);
  }
  _batch(f32) {
    if (!this.stream || !this.ds) return;
    let sum = 0;
    for (let i = 0; i < f32.length; i++) sum += f32[i] * f32[i];
    const rms = Math.sqrt(sum / (f32.length || 1));
    const pcm = this.ds.process(f32);
    this.onframe?.(pcm, rms, (f32.length / ctx.sampleRate) * 1000);
  }
  stop() {
    this.stream?.getTracks().forEach((track) => track.stop());
    if (this.node?.port) {
      try { this.node.port.postMessage("stop"); } catch { /* Already closed. */ }
      this.node.port.onmessage = null;
    }
    if (this.node) this.node.onaudioprocess = null;
    for (const node of [this.src, this.node, this.sink]) {
      try { node?.disconnect(); } catch { /* Already disconnected. */ }
    }
    this.stream = this.src = this.node = this.sink = null;
    this.mode = null;
  }
}

// ------------------------------------------------------------------ speechSynthesis fallback
let speechGen = 0;
const keepAlive = []; // Chrome can GC utterances mid-speech and never fire onend

function pickVoice(lang) {
  const synth = globalThis.speechSynthesis;
  const want = SPEECH_LANG[lang] || "hi-IN";
  const voices = synth?.getVoices?.() || [];
  const norm = (s) => (s || "").replace("_", "-").toLowerCase();
  return voices.find((v) => norm(v.lang) === want.toLowerCase())
    || voices.find((v) => norm(v.lang).startsWith(want.slice(0, 2)))
    || (lang === "mr" ? voices.find((v) => norm(v.lang).startsWith("hi")) : null);
}

/**
 * Speak `text` with the device voice. Long text is split into sentences (some engines cut off
 * after ~15 s). Returns a handle with stop(); onend fires once after the last sentence.
 */
export function speakText(text, lang, { onstart, onend } = {}) {
  const synth = globalThis.speechSynthesis;
  if (!synth || !text) { onend?.(); return { stop() {}, get active() { return false; } }; }
  const gen = ++speechGen;
  synth.cancel();
  const parts = text.split(/(?<=[।.?!])\s+/).map((s) => s.trim()).filter(Boolean);
  const voice = pickVoice(lang);
  let finished = false;
  const finish = () => { if (!finished && gen === speechGen) { finished = true; keepAlive.length = 0; onend?.(); } };
  parts.forEach((p, i) => {
    const u = new SpeechSynthesisUtterance(p);
    u.lang = SPEECH_LANG[lang] || "hi-IN";
    if (voice) u.voice = voice;
    u.rate = 0.95;
    if (i === 0) u.onstart = () => gen === speechGen && onstart?.();
    if (i === parts.length - 1) u.onend = finish;
    u.onerror = (e) => { if (e.error !== "interrupted" && e.error !== "canceled") finish(); };
    keepAlive.push(u);
    synth.speak(u);
  });
  return {
    stop() { if (gen === speechGen) { speechGen++; synth.cancel(); finished = true; keepAlive.length = 0; } },
    get active() { return !finished && gen === speechGen; },
  };
}
// Voices load asynchronously in Chrome; touching the list early warms it up.
globalThis.speechSynthesis?.getVoices?.();
