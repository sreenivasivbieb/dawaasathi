import test from "node:test";
import assert from "node:assert/strict";
import { Mic, playNativeAudio } from "../../web/audio.js";

test("native recording does not wait on a stalled playback context or need worklet callbacks", async () => {
  let stopped = false, recording = false;
  const stream = { getTracks: () => [{ stop() { stopped = true; } }] };
  const before = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: {
    getUserMedia: async () => stream,
  } } });
  globalThis.AudioContext = class {
    state = "suspended"; sampleRate = 48000;
    resume() { return new Promise(() => {}); }
    createMediaStreamSource() { return { disconnect() {} }; }
  };
  globalThis.MediaRecorder = class {
    static isTypeSupported(type) { return type.startsWith("audio/webm"); }
    mimeType = "audio/webm;codecs=opus";
    state = "inactive";
    start() { recording = true; this.state = "recording"; }
    stop() {
      this.state = "inactive";
      this.ondataavailable({ data: new Blob(["recorded audio"]) });
      this.onstop();
    }
  };
  try {
    const mic = new Mic();
    await Promise.race([mic.start(16000), new Promise((_, reject) => setTimeout(() => reject(Error("capture stalled")), 500))]);
    assert.ok(recording);
    assert.equal(mic.mode, "native");
    const blob = await mic.finish();
    assert.equal(await blob.text(), "recorded audio");
    assert.ok(blob.type.startsWith("audio/webm"));
    assert.ok(stopped);
  } finally {
    if (before) Object.defineProperty(globalThis, "navigator", before);
    else delete globalThis.navigator;
    delete globalThis.MediaRecorder;
    delete globalThis.AudioContext;
  }
});

test("native playback reports autoplay rejection instead of pretending to speak", async () => {
  const errors = [];
  let started = false;
  const element = { play: async () => { throw Object.assign(Error("blocked"), { name: "NotAllowedError" }); }, pause() {} };
  const control = playNativeAudio("/reply.wav", { element, onstart() { started = true; }, onerror(e) { errors.push(e.name); } });
  await control.done;
  assert.deepEqual(errors, ["NotAllowedError"]);
  assert.equal(started, false);
  element.onerror();
  assert.equal(errors.length, 1);
  control.stop();
});
