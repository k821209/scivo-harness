// How the page judges "is the session still there" once an unwatched session
// beats every five minutes instead of every five seconds.
// Not part of pytest. Run:  npm i playwright && node tests/browser/heartbeat.spec.mjs
import { chromium } from "playwright";
const PAGE = "file://" + new URL("../../src/scivo_harness/control_page.html", import.meta.url).pathname;
const b = await chromium.launch({ channel: "chrome", args: ["--no-sandbox"] });
let failed = 0;
const check = (name, ok, got) => { console.log(`${ok ? "ok  " : "FAIL"}  ${name}` + (ok ? "" : `   got ${JSON.stringify(got)}`)); if (!ok) failed++; };

async function open() {
  const p = await b.newPage({ viewport: { width: 420, height: 800 } });
  await p.clock.install({ time: new Date("2026-11-10T09:00:00Z") });
  await p.addInitScript(() => {
    const db = {}; const subs = new Map();
    const key = (n, i) => n + "/" + i;
    const notify = (n, i) => { for (const cb of subs.get(key(n, i)) || []) cb(db[n]?.[i] ? { id: i, ...db[n][i] } : null); };
    window.__push = (n, i, doc) => { (db[n] = db[n] || {})[i] = doc; notify(n, i); };
    window.scivo = {
      pid: "p", pubId: "pub", reviewer: "owner",
      list: async () => [], get: async (n, i) => (db[n]?.[i] ? { id: i, ...db[n][i] } : null),
      put: async (n, i, data) => { (db[n] = db[n] || {})[i] = { ...(db[n][i] || {}), ...data }; return data; },
      update: async () => {},
      subscribeDoc: (n, i, cb) => { const k = key(n, i); if (!subs.has(k)) subs.set(k, new Set()); subs.get(k).add(cb); cb(db[n]?.[i] ? { id: i, ...db[n][i] } : null); return () => subs.get(k).delete(cb); },
      subscribe: (n, cb) => { cb([]); return () => {}; },
    };
    window.scivoReady = Promise.resolve(window.scivo);
  });
  await p.goto(PAGE, { waitUntil: "load" });
  await p.clock.runFor(900); await p.waitForTimeout(50);
  return p;
}
const head = (extra) => ({ sid: "s1", rev: 1, state: "live", events: 0, chunk: 20, beat: 1, started: 1, project: "T", project_id: "p", model: "m", ...extra });
const dot = (p) => p.evaluate(() => document.getElementById("sc-dot").className.replace("sc-dot", "").trim());
const pass = async (p, ms) => { await p.clock.runFor(ms); await p.waitForTimeout(50); };

// 1. Opened on a session that is DEAD, whose last head promised five minutes.
{
  const p = await open();
  await p.evaluate((h) => window.__push("content", "head", h), head({ beat: 100, beat_every: 300 }));
  await pass(p, 5000);
  check("just opened: not called dead before it has had a chance to answer", (await dot(p)) === "live", await dot(p));
  await pass(p, 20000);
  check("a session that does not answer the page's own mark is gone in 25 s, not 12 min", (await dot(p)) === "stale", await dot(p));
  await p.close();
}
// 2. Opened on a LIVE idle session: it answers the mark, then keeps a fast beat.
{
  const p = await open();
  await p.evaluate((h) => window.__push("content", "head", h), head({ beat: 100, beat_every: 300 }));
  await pass(p, 300);
  await p.evaluate((h) => window.__push("content", "head", h), head({ rev: 2, beat: 101, beat_every: 5 }));
  await pass(p, 15000);
  check("answered, and 15 s on: connected", (await dot(p)) === "live", await dot(p));
  await pass(p, 10000);
  check("then silent for 25 s at a promised 5 s: gone", (await dot(p)) === "stale", await dot(p));
  await p.close();
}
// 3. Left open; the session says it has gone to the slow beat.
{
  const p = await open();
  await p.evaluate((h) => window.__push("content", "head", h), head({ beat: 100, beat_every: 300 }));
  await pass(p, 300);
  await p.evaluate((h) => window.__push("content", "head", h), head({ rev: 2, beat: 101, beat_every: 5 }));
  await pass(p, 150000);                       // nobody touches the page: marks stop
  await p.evaluate((h) => window.__push("content", "head", h), head({ rev: 3, beat: 102, beat_every: 300 }));
  await pass(p, 5 * 60 * 1000);
  check("five quiet minutes at a promised five minutes: still connected", (await dot(p)) === "live", await dot(p));
  await pass(p, 8 * 60 * 1000);
  check("thirteen minutes: gone", (await dot(p)) === "stale", await dot(p));
  await p.close();
}
// 4. A session from before the field: the old twenty seconds.
{
  const p = await open();
  await p.evaluate((h) => window.__push("content", "head", h), head({ beat: 100 }));
  await pass(p, 300);
  await p.evaluate((h) => window.__push("content", "head", h), head({ rev: 2, beat: 101 }));
  await pass(p, 15000);
  const at15 = await dot(p);
  await pass(p, 10000);
  check("no beat_every: connected at 15 s, gone at 25 s, as before", at15 === "live" && (await dot(p)) === "stale", [at15, await dot(p)]);
  await p.close();
}
await b.close();
process.exit(failed ? 1 : 0);
