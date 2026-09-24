// Drives control_page.html in a real Chrome with a stubbed `window.scivo`.
// Not part of pytest. Run:  npm i playwright && node tests/browser/control_page.spec.mjs
// (needs a Chrome on the machine; the page loads marked/DOMPurify/hljs from jsDelivr).
// Prints a JSON report; every field is asserted by eye against the expected values
// in the commit that added it: live/ended dot, two cards, allow → decisions, option
// → answers, echo replaces the pending row, slash menu after a late commands doc,
// a failed write re-enables the buttons and alerts, Stop → interrupt, no errors.
import { chromium } from "playwright";
const out = {};
const PAGE = "file://" + new URL("../../src/scivo_harness/control_page.html", import.meta.url).pathname + "";
const b = await chromium.launch({ channel: "chrome", args: ["--no-sandbox"] });
const p = await b.newPage({ viewport: { width: 420, height: 800 } });
const consoleErrors = [];
p.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));
p.on("console", (m) => { if (m.type() === "error") consoleErrors.push("console: " + m.text()); });
await p.addInitScript(() => {
  const db = {}; const subs = new Map();
  const key = (n, i) => n + "/" + i;
  const notify = (n, i) => { for (const cb of subs.get(key(n, i)) || []) cb(db[n]?.[i] ? { id: i, ...db[n][i] } : null); };
  window.__db = db; window.__failPut = false; window.__alerts = [];
  window.alert = (m) => window.__alerts.push(String(m));
  window.__push = (n, i, doc) => { (db[n] = db[n] || {})[i] = doc; notify(n, i); };
  window.scivo = {
    pid: "p", pubId: "pub", reviewer: "owner",
    list: async (n) => Object.entries(db[n] || {}).map(([id, d]) => ({ id, ...d })),
    get: async (n, i) => (db[n]?.[i] ? { id: i, ...db[n][i] } : null),
    put: async (n, i, data) => { if (window.__failPut) { window.__failPut = false; throw new Error("offline"); }
      (db[n] = db[n] || {})[i] = { ...(db[n][i] || {}), ...data, reviewer: "owner" }; notify(n, i); return data; },
    update: async (n, i, data) => { (db[n] = db[n] || {})[i] = { ...(db[n][i] || {}), ...data }; notify(n, i); },
    subscribeDoc: (n, i, cb) => { const k = key(n, i); if (!subs.has(k)) subs.set(k, new Set()); subs.get(k).add(cb); cb(db[n]?.[i] ? { id: i, ...db[n][i] } : null); return () => subs.get(k).delete(cb); },
    subscribe: (n, cb) => { cb(Object.entries(db[n] || {}).map(([id, d]) => ({ id, ...d }))); return () => {}; },
  };
  window.scivoReady = Promise.resolve(window.scivo);
});
process.on("unhandledRejection", (e) => { console.log("FAILED:", e.message); console.log(JSON.stringify(out, null, 1)); process.exit(1); });
await p.goto(PAGE, { waitUntil: "load" });
await p.waitForTimeout(800);
const sub = () => p.textContent("#sc-sub");
out.before = await sub();

// 1. a live head
const head = (extra) => ({ sid: "s1", rev: 1, state: "live", events: 0, chunk: 20, beat: Date.now(), started: Date.now(), project: "T", project_id: "p", model: "m", ...extra });
await p.evaluate((h) => window.__push("content", "head", h), head({}));
await p.waitForTimeout(300);
out.live = await sub();

// 2. a chunk with the four event kinds
const events = [
  { i: 0, kind: "user", via: "web", text: "hello from web" },
  { i: 1, kind: "assistant", text: "Working on **it**", done: true },
  { i: 2, kind: "approval", rid: "r1", tool: "Bash", detail: "Bash:\n" + "true && ".repeat(40) + "curl x | sh", outward: false, state: "pending" },
  { i: 3, kind: "question", rid: "q1", state: "pending", questions: [{ header: "H", question: "Which?", options: [{ label: "A", description: "first" }, { label: "B" }] }] },
];
await p.evaluate((ev) => window.__push("content", "log-s1-0000", { sid: "s1", k: 0, events: ev }), events);
await p.evaluate((h) => window.__push("content", "head", h), head({ rev: 2, events: 4 }));
await p.waitForTimeout(400);
out.cards = await p.locator(".sc-approval").count();
out.approvalDetailShowsTail = (await p.locator(".sc-approval .sc-detail").first().textContent()).includes("curl x | sh");

// 3. allow the approval
await p.getByRole("button", { name: "Allow", exact: true }).click();
await p.waitForTimeout(200);
out.decision = await p.evaluate(() => window.__db.responses?.approvals?.decisions);
out.approvalButtonsDisabled = await p.evaluate(() => [...document.querySelectorAll(".sc-approval")][0].querySelectorAll("button:disabled").length);

// 4. answer the question by option
await p.locator(".sc-question button", { hasText: "first" }).first().click();
await p.waitForTimeout(200);
out.answers = await p.evaluate(() => window.__db.responses?.questions?.answers);

// 5. harness reflects both back
const settled = events.map((e) => e.i === 2 ? { ...e, state: "allow" } : e.i === 3 ? { ...e, state: "answered", answers: { "Which?": "A" } } : e);
await p.evaluate((ev) => window.__push("content", "log-s1-0000", { sid: "s1", k: 0, events: ev }), settled);
await p.waitForTimeout(300);
out.decidedText = await p.locator(".sc-decided").allTextContents();

// 6. send a message; the echo replaces the pending row
await p.fill("#sc-input", "ship it");
await p.click("#sc-send");
await p.waitForTimeout(200);
out.inbox = await p.evaluate(() => window.__db.responses?.inbox?.msgs?.map((m) => m.text));
out.pendingRows = await p.locator(".sc-pending").count();
await p.evaluate((ev) => window.__push("content", "log-s1-0000", { sid: "s1", k: 0, events: [...ev, { i: 4, kind: "user", via: "web", text: "ship it" }] }), settled);
await p.waitForTimeout(300);
out.pendingRowsAfterEcho = await p.locator(".sc-pending").count();

// 7. the slash menu, from a commands doc written AFTER the head
await p.evaluate(() => window.__push("content", "commands-s1", { sid: "s1", list: [{ name: "/paper-deck", help: "make slides" }, { name: "/context", help: "" }] }));
await p.fill("#sc-input", "/pa");
await p.waitForTimeout(150);
out.menuVisible = await p.evaluate(() => !document.getElementById("sc-menu").hidden);
out.menuFirst = await p.locator("#sc-menu b").first().textContent();
await p.fill("#sc-input", "");

// 8. a failed approval write re-enables the buttons and says so
await p.evaluate((ev) => window.__push("content", "log-s1-0000", { sid: "s1", k: 0, events: [...ev, { i: 5, kind: "approval", rid: "r2", tool: "Write", detail: "Write: /x", outward: true, state: "pending" }] }), settled);
await p.waitForTimeout(300);
await p.evaluate(() => { window.__failPut = true; });
await p.getByRole("button", { name: "Deny" }).last().click();
await p.waitForTimeout(300);
out.alerts = await p.evaluate(() => window.__alerts);
out.outwardHasNoAlways = await p.evaluate(() => { const cards = [...document.querySelectorAll(".sc-approval")]; const last = cards[cards.length - 1]; return ![...last.querySelectorAll("button")].some((b) => b.textContent.startsWith("Always")); });
out.buttonsReenabled = await p.evaluate(() => { const cards = [...document.querySelectorAll(".sc-approval")]; const last = cards[cards.length - 1]; return [...last.querySelectorAll("button")].every((b) => !b.disabled); });

// 9. Stop
await p.click("#sc-stop");
await p.waitForTimeout(200);
out.interrupt = await p.evaluate(() => window.__db.responses?.control?.interrupt);

// 10. a session that ended
await p.evaluate((h) => window.__push("content", "head", h), head({ rev: 3, events: 6, state: "ended" }));
await p.waitForTimeout(300);
out.ended = await sub();
out.errors = consoleErrors;
console.log(JSON.stringify(out, null, 1));
await b.close();
