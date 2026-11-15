// Real browser/native capture and playback with WebAudio deliberately unavailable.
// The input is a synthetic WAV file; no physical microphone or room audio is recorded.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const base = process.env.DAWA_CHECK_URL || "http://127.0.0.1:8004";
const sample = process.env.DAWA_MIC_SAMPLE;
if (!sample) throw Error("DAWA_MIC_SAMPLE must point to a synthetic Hindi question WAV.");
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium", headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", `--use-file-for-fake-audio-capture=${sample}%noloop`] });
const context = await browser.newContext({ permissions: ["microphone"], viewport: { width: 1440, height: 1050 } });
const page = await context.newPage();
const report = { synthetic_microphone: true, physical_microphone_verified: false, web_audio_unavailable: true, checks: [], page_errors: [], passed: false };
const sessions = new Set();
page.on("pageerror", e => report.page_errors.push(e.message));
await page.addInitScript(() => {
  window.AudioContext = undefined;
  window.webkitAudioContext = undefined;
});
try {
  await page.goto(base, { waitUntil: "networkidle" });
  await page.locator("#mic:not([disabled])").waitFor();
  const typed = page.waitForResponse(r => r.url().endsWith("/v1/query"));
  await page.locator("#q").fill("hello");
  await page.locator("#q").press("Enter");
  const greeting = await (await typed).json(); sessions.add(greeting.session_id);
  assert.equal(greeting.card.title, "GREETING");
  assert.equal(greeting.candidates.length, 0);
  await page.waitForFunction(() => {
    const p = document.querySelector("#reply-player");
    return p && !p.paused && p.currentTime > 0 && p.readyState >= 2;
  }, null, { timeout: 30000 });
  report.checks.push({ name: "typed hello gives a greeting and actual native playback advances", passed: true });
  await page.locator('#result [data-act="stop-audio"]').click();

  await page.reload({ waitUntil: "networkidle" });
  await page.locator('#result [data-act="replay"]').waitFor();
  const replayed = page.waitForResponse(r => r.url().endsWith("/v1/speak"));
  await page.locator('#result [data-act="replay"]').click();
  assert.equal((await replayed).status(), 200);
  await page.waitForFunction(() => document.querySelector("#mic").dataset.state === "speaking", null, { timeout: 30000 });
  report.checks.push({ name: "saved-history Replay uses server speech instead of absent OS speech voices", passed: true });
  await page.locator("#voice-stop").click();

  const recognized = page.waitForResponse(r => r.url().endsWith("/v1/query"), { timeout: 45000 });
  let uploadedType;
  page.on("request", r => { if (r.url().endsWith("/v1/query")) uploadedType = r.headers()["content-type"]; });
  await page.locator("#mic").click();
  await page.waitForFunction(() => document.querySelector("#mic").dataset.state === "recording");
  await page.waitForTimeout(4500); // let the synthetic question enter the browser-native recorder
  await page.locator("#mic").click();
  const voice = await (await recognized).json(); sessions.add(voice.session_id);
  assert.equal(voice.detected_language, "hi");
  assert.equal(voice.match.brand, "Dolo 650 Tablet");
  assert.ok(voice.transcript);
  assert.ok(uploadedType.startsWith("multipart/form-data"));
  await page.waitForFunction(() => {
    const p = document.querySelector("#reply-player");
    return p && !p.paused && p.currentTime > 0 && p.readyState >= 2;
  }, null, { timeout: 30000 });
  assert.equal(await page.evaluate(() => typeof window.AudioContext), "undefined");
  report.checks.push({ name: "native microphone to live STT to live TTS works without WebAudio", transcript: voice.transcript, passed: true });
  await mkdir("artifacts", { recursive: true });
  await page.screenshot({ path: "artifacts/native-browser-voice.png", fullPage: true });
  await page.locator("#voice-stop").click();
  assert.deepEqual(report.page_errors, []);
  report.passed = true;
} finally {
  for (const sid of sessions) await page.request.delete(base + "/v1/threads/" + sid);
  await mkdir("artifacts", { recursive: true });
  await writeFile("artifacts/native-browser-voice.json", JSON.stringify(report, null, 2) + "\n");
  await browser.close();
}
console.log(JSON.stringify(report, null, 2));
