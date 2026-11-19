// A card the session is waiting on is on the screen wherever it is in the log,
// and the page says the session is waiting (CRE_genemodel, 2026-11-19: a card
// 78 events up a log of 310, on a page that loads the last forty; the session
// answered nothing else and looked frozen).
// Not part of pytest. Run:  npm i playwright && node tests/browser/waiting.spec.mjs
import { chromium } from "playwright";
const PAGE = "file://" + new URL("../../src/scivo_harness/control_page.html", import.meta.url).pathname;
const b = await chromium.launch({ channel: "chrome", args: ["--no-sandbox"] });
let failed = 0;
const check = (name, ok, got) => { console.log(`${ok ? "ok  " : "FAIL"}  ${name}` + (ok ? "" : `   got ${JSON.stringify(got)}`)); if (!ok) failed++; };

async function open(width = 420) {
  const p = await b.newPage({ viewport: { width, height: 800 } });
  p.on("pageerror", (e) => { console.log("FAIL  page error: " + e.message); failed++; });
  await p.addInitScript(() => {
    const db = {}; const subs = new Map(); const fetched = [];
    const key = (n, i) => n + "/" + i;
    const notify = (n, i) => { for (const cb of subs.get(key(n, i)) || []) cb(db[n]?.[i] ? { id: i, ...db[n][i] } : null); };
    window.__push = (n, i, doc) => { (db[n] = db[n] || {})[i] = doc; notify(n, i); };
    window.__db = db; window.__fetched = fetched;
    window.__listeners = () => Object.fromEntries([...subs].map(([k, v]) => [k, v.size]));
    window.scivo = {
      pid: "p", pubId: "pub", reviewer: "owner",
      list: async () => [], get: async (n, i) => { fetched.push(i); return db[n]?.[i] ? { id: i, ...db[n][i] } : null; },
      put: async (n, i, data) => { (db[n] = db[n] || {})[i] = { ...(db[n][i] || {}), ...data }; return data; },
      update: async () => {},
      subscribeDoc: (n, i, cb) => { fetched.push(i); const k = key(n, i); if (!subs.has(k)) subs.set(k, new Set()); subs.get(k).add(cb); cb(db[n]?.[i] ? { id: i, ...db[n][i] } : null); return () => subs.get(k).delete(cb); },
      subscribe: (n, cb) => { cb([]); return () => {}; },
    };
    window.scivoReady = Promise.resolve(window.scivo);
  });
  await p.goto(PAGE, { waitUntil: "load" });
  await p.waitForTimeout(1100);
  return p;
}
const chunkId = (k) => "log-s1-" + String(k).padStart(4, "0");
const filler = (k) => Array.from({ length: 20 }, (_, j) => ({ i: k * 20 + j, kind: "status", text: `event ${k * 20 + j}`, t: 1 }));
const QUESTION = "run run_20261118_111955 in _project/annot-build-v61 finished, exit 0 — pick it up?";
const card = (state, extra = {}) => ({ i: 232, kind: "question", rid: "r232", state, t: 1,
  questions: [{ question: QUESTION, header: "background task", multiSelect: false,
    options: [{ label: "Pick it up", description: "start a turn on it now" }, { label: "Not now", description: "leave it" }] }], ...extra });
const head = (extra) => ({ sid: "s1", rev: 1, state: "live", events: 310, chunk: 20, beat: Date.now(), started: 1,
  project: "CRE_genemodel", project_id: "p", model: "m", beat_every: 5, ...extra });
const bar = (p) => p.evaluate(() => { const el = document.getElementById("sc-waiting"); return el.hidden ? null : el.textContent; });

async function seed(p, cardEvent, headExtra) {
  await p.evaluate(({ ids, cardEvent, h }) => {
    for (const [k, events] of ids) window.__push("content", "log-s1-" + String(k).padStart(4, "0"), { events });
    window.__push("content", "head", h);
  }, { ids: [[11, filler(11).map((e) => (e.i === 232 ? cardEvent : e))], [12, filler(12)], [13, filler(13)],
            [14, filler(14)], [15, filler(15).slice(0, 10)]], cardEvent, h: head(headExtra) });
  await p.waitForTimeout(600);
}

// 1. A card far up the log, on a freshly opened page.
{
  const p = await open();
  await seed(p, card("pending"), { waiting: [{ i: 232, kind: "question" }] });
  const text = await bar(p);
  check("the page says the session is waiting, over the message box", !!text && text.includes("waiting for you") && text.includes("question"), text);
  check("the card is on the page though it is 78 events back", await p.evaluate((q) => document.getElementById("sc-log").innerText.includes(q), QUESTION), "");
  check("with its buttons", (await p.locator(".sc-approval button", { hasText: "Not now" }).count()) === 1, "");
  check("and the chunks in between were not fetched for it",
    await p.evaluate(() => !document.getElementById("sc-log").innerText.includes("event 245") && !window.__fetched.includes("log-s1-0012")), await p.evaluate(() => window.__fetched));
  check("the newest events are there as before", await p.evaluate(() => document.getElementById("sc-log").innerText.includes("event 309")), "");
  // This stand-in answers before `subscribeDoc` returns, as a client holding
  // the document already does. That used to throw; and a settled chunk must
  // still be let go of, once, with nothing listened to twice.
  const listeners = await p.evaluate(() => window.__listeners());
  check("a chunk that is finished is not listened to any more", listeners["content/log-s1-0014"] === 0, listeners);
  check("and nothing is listened to twice", Object.values(listeners).every((n) => n <= 1), listeners);
  check("the card's chunk stays listened to while it waits", listeners["content/log-s1-0011"] === 1, listeners);
  await p.click("#sc-waiting");
  await p.waitForTimeout(300);
  const seen = await p.evaluate(() => { const el = document.querySelector(".sc-approval"); const r = el.getBoundingClientRect(); return { flash: el.classList.contains("sc-flash"), inView: r.top >= 0 && r.bottom <= window.innerHeight }; });
  check("pressing the bar goes to the card", seen.flash && seen.inView, seen);
  await p.locator(".sc-approval button", { hasText: "Not now" }).click();
  await p.waitForTimeout(300);
  const sent = await p.evaluate(() => window.__db.responses?.questions);
  check("answering it there reaches the session", !!sent && sent.sid === "s1" && JSON.stringify(sent.answers).includes("Not now"), sent);
  // The session takes the answer: the card settles and the head stops listing it.
  await p.evaluate(({ c, h }) => {
    const doc = window.__db.content["log-s1-0011"]; doc.events = doc.events.map((e) => (e.i === 232 ? c : e));
    window.__push("content", "log-s1-0011", doc); window.__push("content", "head", h);
  }, { c: card("answered", { answers: { [QUESTION]: "Not now" } }), h: head({ rev: 2, waiting: [] }) });
  await p.waitForTimeout(400);
  check("the bar goes when nothing is waiting", (await bar(p)) === null, await bar(p));
  await p.close();
}
// 2. A card the session stopped waiting on says why.
{
  const p = await open();
  await seed(p, card("skipped", { answers: null, note: "set aside — a message was typed instead of an answer." }), { waiting: [] });
  await p.evaluate((h) => window.__push("content", "head", h), head({ rev: 2, waiting: [{ i: 232, kind: "question" }] }));
  await p.waitForTimeout(300);
  await p.evaluate((h) => window.__push("content", "head", h), head({ rev: 3, waiting: [] }));
  await p.waitForTimeout(300);
  const text = await p.evaluate(() => document.querySelector(".sc-approval")?.innerText || "");
  check("a card that was set aside says so, and offers no buttons", text.includes("set aside — a message was typed") && !text.includes("skip this question"), text);
  check("and nothing is said to be waiting", (await bar(p)) === null, await bar(p));
  await p.close();
}
// 3. An approval, and more than one card; a narrow screen; an ended session.
{
  const p = await open(280);
  await seed(p, card("pending"), { waiting: [{ i: 232, kind: "approval" }, { i: 300, kind: "question" }] });
  const text = await bar(p);
  check("an approval is named as one, and the rest counted", !!text && text.includes("a tool to be approved") && text.includes("and 1 more"), text);
  const fits = await p.evaluate(() => { const el = document.getElementById("sc-waiting"); const r = el.getBoundingClientRect(); return el.scrollWidth <= el.clientWidth + 1 && r.right <= window.innerWidth + 1; });
  check("the bar fits at 280px", fits, fits);
  await p.evaluate((h) => window.__push("content", "head", h), head({ rev: 2, state: "ended", waiting: [{ i: 232, kind: "approval" }] }));
  await p.waitForTimeout(300);
  check("an ended session is waiting on nobody", (await bar(p)) === null, await bar(p));
  await p.close();
}
// 4. A head from a session that does not say (an older harness): no bar, nothing breaks.
{
  const p = await open();
  await seed(p, card("pending"), {});
  check("no bar without the session saying so", (await bar(p)) === null, await bar(p));
  check("and the page still shows the newest events", await p.evaluate(() => document.getElementById("sc-log").innerText.includes("event 309")), "");
  await p.close();
}
await b.close();
console.log(failed ? `\n${failed} FAILED` : "\nall passed");
process.exit(failed ? 1 : 0);
