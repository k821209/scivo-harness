// The page says "someone is here" when it is being USED, and says what a
// message is waiting for when the session has been idle.
// Not part of pytest. Run:  npm i playwright && node tests/browser/presence.spec.mjs
// Exits non-zero on the first thing that is not as stated; every check prints.
import { chromium } from "playwright";
const PAGE = "file://" + new URL("../../src/scivo_harness/control_page.html", import.meta.url).pathname;
const b = await chromium.launch({ channel: "chrome", args: ["--no-sandbox"] });
const p = await b.newPage({ viewport: { width: 420, height: 800 } });
const errors = [];
p.on("pageerror", (e) => errors.push(e.message));
await p.clock.install({ time: new Date("2026-11-10T09:00:00Z") });
await p.addInitScript(() => {
  const db = {}; const subs = new Map();
  const key = (n, i) => n + "/" + i;
  const notify = (n, i) => { for (const cb of subs.get(key(n, i)) || []) cb(db[n]?.[i] ? { id: i, ...db[n][i] } : null); };
  window.__db = db; window.__puts = [];
  window.__push = (n, i, doc) => { (db[n] = db[n] || {})[i] = doc; notify(n, i); };
  window.scivo = {
    pid: "p", pubId: "pub", reviewer: "owner",
    list: async (n) => Object.entries(db[n] || {}).map(([id, d]) => ({ id, ...d })),
    get: async (n, i) => (db[n]?.[i] ? { id: i, ...db[n][i] } : null),
    put: async (n, i, data) => { window.__puts.push([n, i, Object.keys(data).sort().join(",")]);
      (db[n] = db[n] || {})[i] = { ...(db[n][i] || {}), ...data, reviewer: "owner" }; notify(n, i); return data; },
    update: async () => {},
    subscribeDoc: (n, i, cb) => { const k = key(n, i); if (!subs.has(k)) subs.set(k, new Set()); subs.get(k).add(cb); cb(db[n]?.[i] ? { id: i, ...db[n][i] } : null); return () => subs.get(k).delete(cb); },
    subscribe: (n, cb) => { cb([]); return () => {}; },
  };
  window.scivoReady = Promise.resolve(window.scivo);
});
let failed = 0;
const check = (name, ok, got) => { console.log(`${ok ? "ok  " : "FAIL"}  ${name}` + (ok ? "" : `   got ${JSON.stringify(got)}`)); if (!ok) failed++; };
const marks = () => p.evaluate(() => window.__puts.filter((x) => x[1] === "control" && x[2].includes("viewer")).length);
const control = () => p.evaluate(() => window.__db.responses?.control || null);
const head = (extra) => ({ sid: "s1", rev: 1, state: "live", events: 0, chunk: 20, beat: Date.now(), started: Date.now(), project: "T", project_id: "p", model: "m", ...extra });
const settle = async (ms = 400) => { await p.clock.runFor(ms); await p.waitForTimeout(50); };

await p.goto(PAGE, { waitUntil: "load" });
await settle(900);
check("nothing is written before a session is known", (await marks()) === 0, await marks());

await p.evaluate((h) => window.__push("content", "head", h), head({ poll_s: 15 }));
await settle();
check("opening the page on a live session marks someone present, once", (await marks()) === 1, await marks());
const first = await control();
check("the mark names the session and is the control document", first && first.sid === "s1" && first.doc === "control" && typeof first.viewer === "number", first);

// Typing straight away does not write again: one mark per 30 s is enough.
await p.click("#sc-input"); await p.keyboard.type("hello");
await settle();
check("further keystrokes inside 30 s write nothing more", (await marks()) === 1, await marks());

// The idle hint, from the head's own figure.
await p.keyboard.press("Enter");
await settle();
const pendingText = await p.locator(".sc-pending .sc-via").first().textContent();
check("a message sent to an idle session says what it waits for", /idle/.test(pendingText) && /15 s/.test(pendingText), pendingText);

// Stop, then a later mark: `interrupt` must survive the merge.
await p.evaluate(() => { const b = [...document.querySelectorAll("button")].find((x) => /stop/i.test(x.textContent) || x.id === "sc-stop"); if (b) b.click(); });
await settle();
const stopped = await control();
await p.clock.runFor(31000); await p.waitForTimeout(50);
await p.keyboard.type("x"); await settle();
const after = await control();
check("still in use after 30 s: a second mark", (await marks()) === 2, await marks());
check("Stop's counter is kept beside the mark", (stopped?.interrupt || 0) >= 1 ? after.interrupt === stopped.interrupt : true, [stopped, after]);
check("…and Stop was actually pressed in this run", (stopped?.interrupt || 0) >= 1, stopped);

// Left open and untouched: the marks stop.
const before = await marks();
await p.clock.runFor(10 * 60 * 1000); await p.waitForTimeout(50);
check("ten untouched minutes write no more than the marks already owed", (await marks()) - before <= 4, (await marks()) - before);
const idleCount = await marks();
await p.clock.runFor(30 * 60 * 1000); await p.waitForTimeout(50);
check("a tab left open for another half hour writes nothing at all", (await marks()) === idleCount, (await marks()) - idleCount);

// Coming back is one keystroke.
await p.keyboard.type("y"); await settle();
check("the first touch after that marks present again", (await marks()) === idleCount + 1, (await marks()) - idleCount);

// A busy session: no hint.
await p.evaluate((h) => window.__push("content", "head", h), head({ rev: 3, poll_s: 0.15 }));
await settle();
await p.fill("#sc-input", "second"); await p.keyboard.press("Enter"); await settle();
const busyText = await p.locator(".sc-pending .sc-via").last().textContent();
check("a message to a session that is reading fast carries no hint", busyText.trim() === "you · web · sending…", busyText);

check("no page errors", errors.length === 0, errors);
await b.close();
process.exit(failed ? 1 : 0);
