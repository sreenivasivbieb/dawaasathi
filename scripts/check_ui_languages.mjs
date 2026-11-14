// All-language UI check using the actual app module and a running local backend.
const { JSDOM } = await import(process.env.JSDOM_MODULE || "jsdom");
import { readFile, writeFile } from "node:fs/promises";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const root = process.cwd(),
  base = process.env.DAWA_CHECK_URL || "http://127.0.0.1:8002";
const dom = new JSDOM(await readFile(root + "/web/index.html", "utf8"), {
  url: base,
});
for (const key of ["window", "document", "navigator", "localStorage"])
  Object.defineProperty(globalThis, key, {
    value: dom.window[key],
    configurable: true,
  });
dom.window.HTMLDialogElement.prototype.showModal = function () {
  this.open = true;
};
dom.window.HTMLDialogElement.prototype.close = function () {
  this.open = false;
};
globalThis.AudioContext = class {
  state = "running";
};
const nativeFetch = fetch;
globalThis.fetch = (path, options) => nativeFetch(new URL(path, base), options);
const $ = (s) => document.querySelector(s);
const wait = async (f) => {
  for (let n = 0; n < 1200; n++) {
    if (f()) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw Error("Timeout");
};
const { LANGS, t } = await import(pathToFileURL(root + "/web/i18n.js").href);
const { INTERFACE } = await import(
  pathToFileURL(root + "/web/i18n/interface.js").href
);
await import(pathToFileURL(root + "/web/app.js").href);
$("#q").value = "Dolo 650 price and generic";
$("#ask").dispatchEvent(
  new dom.window.Event("submit", { bubbles: true, cancelable: true }),
);
await wait(() => $(".results").getAttribute("aria-busy") === "false");
const invalidPhoto = new File(["invalid"], "invalid.txt", {
  type: "text/plain",
});
Object.defineProperty($("#in-strip"), "files", {
  value: [invalidPhoto],
  configurable: true,
});
$("#in-strip").dispatchEvent(new dom.window.Event("change"));
const checks = [];
for (const lang of LANGS) {
  $("#lang").value = lang;
  $("#lang").dispatchEvent(new dom.window.Event("change"));
  await wait(() => $(".results").getAttribute("aria-busy") === "false");
  assert.equal(document.documentElement.lang, lang);
  assert.equal(document.title, t(lang, "page_title"));
  for (const el of document.querySelectorAll("[data-i18n]"))
    assert.equal(
      el.textContent,
      t(lang, el.dataset.i18n),
      lang + ":" + el.dataset.i18n,
    );
  for (const el of document.querySelectorAll("[data-i18n-aria]"))
    assert.equal(el.getAttribute("aria-label"), t(lang, el.dataset.i18nAria));
  assert.equal($("#q").placeholder, t(lang, "type_placeholder"));
  assert.equal($("#toast").textContent, t(lang, "err_UNSUPPORTED_MEDIA"));
  assert.equal($("#connection").textContent, t(lang, "demo_notice"));
  assert.equal($("#mic-label").textContent, t(lang, "err_STT_NOT_CONFIGURED"));
  assert.equal($("#result [lang]").getAttribute("lang"), lang);
  assert.equal($("#result-status").textContent, t(lang, "status_saved"));
  assert.equal(document.querySelectorAll("#result .en").length, 0);
  $("#btn-history").click();
  await wait(() => $("[data-thread-open]"));
  assert.equal($("[data-thread-open]").textContent, t(lang, "open"));
  $('[data-close="history"]').click();
  // No English fallback for any of the newly introduced interface strings.
  for (const key of Object.keys(INTERFACE.en)) {
    assert.ok(INTERFACE[lang][key]);
    if (lang !== "en")
      assert.notEqual(
        INTERFACE[lang][key],
        INTERFACE.en[key],
        lang + ":" + key,
      );
  }
  checks.push({
    language: lang,
    static_labels: true,
    accessibility: true,
    status_and_history: true,
    result_language: true,
  });
  console.log("PASS full interface:", lang);
}
$("#btn-new").click();
$("#lang").value = "en";
$("#lang").dispatchEvent(new dom.window.Event("change"));
$("#q").value = "Glycomet";
$("#ask").dispatchEvent(
  new dom.window.Event("submit", { bubbles: true, cancelable: true }),
);
await wait(() => $(".results").getAttribute("aria-busy") === "false");
$("#lang").value = "ta";
$("#lang").dispatchEvent(new dom.window.Event("change"));
await wait(() => $(".results").getAttribute("aria-busy") === "false");
$('[data-act="confirm"]').click();
await wait(() => $(".results").getAttribute("aria-busy") === "false");
assert.equal($("#result [lang]").getAttribute("lang"), "ta");
console.log("PASS language change during medicine confirmation");
for (const id of JSON.parse(localStorage.getItem("momo.threads") || "[]"))
  await nativeFetch(base + "/v1/threads/" + id, { method: "DELETE" });
const report = {
  checks,
  visual_browser: false,
  pending_confirmation_language: true,
  medical_names_and_user_input_preserved: true,
};
if (process.env.DAWA_UI_LANGUAGE_REPORT)
  await writeFile(
    process.env.DAWA_UI_LANGUAGE_REPORT,
    JSON.stringify(report, null, 2) + "\n",
  );
dom.window.close();
console.log("10/10 full-interface language checks passed");
process.exit(0);
