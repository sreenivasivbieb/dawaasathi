import test from "node:test";
import assert from "node:assert/strict";
import { titleKey, renderResult } from "../../web/render.js";

test("known medicine with historical safety data stays identified and shows unverified safety", () => {
  const result = {
    status: "unverified", verdict: { code: "UNVERIFIED", mode: "lookup", facts: { banned_check: "unverified" } },
    safety: { status: "unverified", reason: "banned_list_stale", list_as_of: "2021-11-22" },
    items: { focus: { brand: "Dolo 650 Tablet", salts: [{ name: "paracetamol", amount: 650, unit: "mg" }] } },
    script: { text: "Dolo 650 contains Paracetamol 650 mg. The banned-list check could not be completed." },
    flags: [], alternatives: [], data_sources: {},
  };
  assert.equal(titleKey(result), "MATCH_lookup");
  const html = String(renderResult(result, { lang: "en" }));
  assert.ok(html.includes("Medicine identified"));
  assert.ok(html.includes("Could not verify"));
  assert.equal(result.status, "unverified");
  assert.equal(titleKey({ ...result, verdict: { ...result.verdict, facts: { missing: ["strength"] } } }), "UNVERIFIED");
});

test("a confirmed candidate is labelled as the user's selection alongside the original transcript", () => {
  const result = {
    status: "unverified", transcript: "unclear name", match: { brand: "Example 500mg Tablet", confirmed: true, confidence: 0.682 },
    verdict: { code: "UNVERIFIED", mode: "lookup", facts: { banned_check: "unverified" } },
    script: { text: "Example 500mg Tablet" }, items: {}, flags: [], alternatives: [], data_sources: {},
  };
  const html = String(renderResult(result, { lang: "en", compact: true }));
  assert.ok(html.includes("Medicine selected"));
  assert.ok(html.includes("You selected"));
  assert.ok(html.includes("Example 500mg Tablet"));
  assert.ok(html.includes("unclear name"));
  assert.ok(!html.includes("Medicine identified"));
});
