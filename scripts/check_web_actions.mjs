// DOM interaction checks with actual backend requests; no browser or visual verification.
// Install the check-only dependency outside the project:
//   npm install --prefix /tmp/dawa-ui-check jsdom
// Run from the repo root with a test instance running at :8001:
//   JSDOM_MODULE=/tmp/dawa-ui-check/node_modules/jsdom/lib/api.js node scripts/check_web_actions.mjs
// Uses synthetic conversations and removes only the threads it creates.
const { JSDOM } = await import(process.env.JSDOM_MODULE || "jsdom");
import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const root = process.cwd();
const base = process.env.DAWA_CHECK_URL || "http://127.0.0.1:8001";
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
const errors = [];
process.on("unhandledRejection", (e) => errors.push(e));
const nativeFetch = globalThis.fetch;
let failNext = false;
globalThis.fetch = (path, options) => {
  if (failNext && path === "/v1/query") {
    failNext = false;
    throw new Error("simulated connection loss");
  }
  return nativeFetch(new URL(path, base), options);
};
const $ = (selector) => document.querySelector(selector);
const wait = async (predicate) => {
  for (let i = 0; i < 1000; i++) {
    if (predicate()) return;
    await new Promise((r) => setTimeout(r, 20));
  }
  throw new Error("Timed out: " + predicate);
};
const idle = () =>
  wait(() => $(".results").getAttribute("aria-busy") === "false");
let checks = 0;
const results = [];
function check(label, callback) {
  callback();
  checks++;
  results.push({ name: label, passed: true });
  console.log("PASS", label);
}
await import(pathToFileURL(root + "/web/app.js").href);
check("empty start has no seeded consultation or prices", () => {
  assert.equal($("#result").textContent.trim(), "");
  assert.doesNotMatch($("#result").textContent, /₹|appointment|Approved/);
});
check("unconfigured microphone disabled; demo data disclosed", () => {
  assert.equal($("#mic").disabled, true);
  assert.equal($("#mic").hidden, false);
  assert.equal($("#voice-setup").hidden, false);
  assert.match($("#connection").textContent, /Demo data/);
});
check("secondary tools and promotional blocks removed", () => {
  for (const selector of [
    "#btn-pouch",
    "#pouch",
    "#draft",
    "#grievance",
    "#modes",
    ".examples",
    ".empty",
    ".step",
  ])
    assert.equal($(selector), null, selector);
  assert.equal($("#conversation").hidden, true);
});
async function submit(text) {
  $("#q").value = text;
  $("#ask").dispatchEvent(
    new dom.window.Event("submit", { bubbles: true, cancelable: true }),
  );
  await idle();
}
await submit("Glycomet");
check("ambiguous medicine has working candidate actions", () =>
  assert.ok($('[data-act="confirm"]')),
);
$('[data-act="confirm"]').click();
await idle();
check("candidate choice resolves real backend result", () =>
  assert.match($("#result").textContent, /Metformin/),
);
$('[data-act="chemist-toggle"]').click();
check("chemist action expands backend script", () =>
  assert.equal($("#chemist-panel").hidden, false),
);
$("#btn-history").click();
await wait(() => $("[data-thread-open]"));
$("[data-thread-open]").click();
await idle();
check("history reopens actual saved conversation", () => {
  assert.match($("#thread").textContent, /Glycomet/);
  assert.match($("#result-status").textContent, /Saved result/);
  assert.equal($("#draft"), null);
});
$("#lang").value = "hi";
$("#lang").dispatchEvent(new dom.window.Event("change"));
check("language change updates labels and existing result", () => {
  assert.equal(document.documentElement.lang, "hi");
  assert.equal($("#send").textContent, "भेजें");
  assert.match($("#result").textContent, /दवा|साल्ट/);
});
$("#lang").value = "en";
$("#lang").dispatchEvent(new dom.window.Event("change"));
$("#btn-new").click();
check("new conversation clears previous result", () =>
  assert.equal($("#result").textContent.trim(), ""),
);
failNext = true;
await submit("Dolo 650 price");
check("network failure retains input and displays retryable error", () => {
  assert.equal($("#q").value, "Dolo 650 price");
  assert.equal($("#request-error").hidden, false);
  assert.equal($("#send").disabled, false);
});
await submit("Dolo 650 price");
check("retry succeeds and clears error", () => {
  assert.match($("#result").textContent, /Dolo 650/);
  assert.equal($("#request-error").hidden, true);
});
$("#btn-new").click();
const photo = new File(
  [await readFile(root + "/samples/strip_augmentin_625.png")],
  "strip_augmentin_625.png",
  { type: "image/png" },
);
Object.defineProperty($("#in-strip"), "files", {
  value: [photo],
  configurable: true,
});
$("#in-strip").dispatchEvent(new dom.window.Event("change"));
check("photo selection displays removable attachment", () =>
  assert.ok($('[data-act="remove-photo"]')),
);
await submit("");
check(
  "real uploaded photo renders identified medicine and clears attachment",
  () => {
    assert.match($("#result").textContent, /Augmentin 625/);
    assert.equal($("#thumbs").children.length, 0);
  },
);
check("supporting details are collapsed; no pouch or symptom controls", () => {
  assert.ok($(".result-details"));
  assert.equal($(".result-details").open, false);
  assert.equal($('[data-act="pouch-add"]'), null);
  assert.equal($('[data-act="symptom"]'), null);
});
$("#btn-new").click();
for (const [kind, name] of [
  ["strip", "strip_glycomet_sr_500.png"],
  ["rx", "rx_glycomet_500.png"],
]) {
  const file = new File([await readFile(root + "/samples/" + name)], name, {
    type: "image/png",
  });
  Object.defineProperty($("#in-" + kind), "files", {
    value: [file],
    configurable: true,
  });
  $("#in-" + kind).dispatchEvent(new dom.window.Event("change"));
}
await submit("");
check(
  "two real photos show mismatch and comparison without expanding details",
  () => {
    assert.ok($(".cmp"));
    assert.ok($(".card--red"));
    assert.match($("#result").textContent, /sustained|SR/);
  },
);
$("#btn-history").click();
await wait(() => $("[data-thread-delete]"));
const before = document.querySelectorAll("[data-thread-delete]").length;
$("[data-thread-delete]").click();
await wait(
  () => document.querySelectorAll("[data-thread-delete]").length === before - 1,
);
await idle();
check("history deletion refreshes list", () =>
  assert.equal(
    document.querySelectorAll("[data-thread-delete]").length,
    before - 1,
  ),
);
// Delete only conversation ids created in this isolated check.
for (const id of JSON.parse(localStorage.getItem("momo.threads") || "[]"))
  await nativeFetch(base + "/v1/threads/" + id, { method: "DELETE" });
check("no unhandled application errors", () => assert.deepEqual(errors, []));
const report = {
  dom_checks: checks,
  backend: base,
  visual_browser: false,
  checks: results,
};
if (process.env.DAWA_WEB_REPORT)
  await (
    await import("node:fs/promises")
  ).writeFile(
    process.env.DAWA_WEB_REPORT,
    JSON.stringify(report, null, 2) + "\n",
  );
console.log(
  JSON.stringify({ dom_checks: checks, backend: base, visual_browser: false }),
);
dom.window.close();
process.exit(0);
