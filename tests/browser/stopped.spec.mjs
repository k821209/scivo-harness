// What the page says when its session has stopped answering, and what becomes
// of a message typed at it (feedback 515b505746ee, 2026-11-18: "이 웹 하네스
// 채팅도 멈췄었어"). The session had been stopped for ten hours; the page read
// "connected" for most of every half minute its owner spent typing at it.
// Not part of pytest. Run:  npm i playwright && node tests/browser/stopped.spec.mjs
import { chromium } from "playwright";
const PAGE = "file://" + new URL("../../src/scivo_harness/control_page.html", import.meta.url).pathname;
const b = await chromium.launch({ channel: "chrome", args: ["--no-sandbox"] });
let failed = 0;
const check = (name, ok, got) => { console.log(`${ok ? "ok  " : "FAIL"}  ${name}` + (ok ? "" : `   got ${JSON.stringify(got)}`)); if (!ok) failed++; };

async function open() {
  const p = await b.newPage({ viewport: { width: 420, height: 800 } });
  await p.clock.install({ time: new Date("2026-11-17T22:06:00Z") });
  await p.addInitScript(() => {
    const db = {}; const subs = new Map();
    const key = (n, i) => n + "/" + i;
    const notify = (n, i) => { for (const cb of subs.get(key(n, i)) || []) cb(db[n]?.[i] ? { id: i, ...db[n][i] } : null); };
    window.__push = (n, i, doc) => { (db[n] = db[n] || {})[i] = doc; notify(n, i); };
    window.__db = db;
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
const head = (extra) => ({ sid: "s1", rev: 1, state: "live", events: 2, chunk: 20, beat: 1, started: 1, project: "T", project_id: "p", model: "m", beat_every: 5, ...extra });
const dot = (p) => p.evaluate(() => document.getElementById("sc-dot").className.replace("sc-dot", "").trim());
const sub = (p) => p.evaluate(() => document.getElementById("sc-sub").textContent);
const pending = (p) => p.evaluate(() => [...document.querySelectorAll(".sc-pending .sc-via")].map((n) => n.textContent));
const pass = async (p, ms) => { await p.clock.runFor(ms); await p.waitForTimeout(50); };
const say = async (p, text) => { await p.fill("#sc-input", text); await p.keyboard.press("Enter"); };
const talk = (p) => p.evaluate(() => window.__push("content", "log-s1-0000", { events: [
  { i: 0, kind: "user", via: "web", text: "정리해줘", t: 1 }, { i: 1, kind: "assistant", done: true, text: "정리했습니다.", t: 2 }] }));

// 1. The session stops beating. Ten hours later its owner comes back and types.
{
  const p = await open();
  await talk(p);
  await p.evaluate((h) => window.__push("content", "head", h), head({}));
  await pass(p, 3000);
  check("while it beats: connected", (await dot(p)) === "live", await sub(p));
  await pass(p, 10 * 3600e3 + 13 * 60e3);
  check("ten hours of silence, in words a person reads", /^not answering for 10 h 13 min — /.test(await sub(p)), await sub(p));
  check("and what to do about it", (await sub(p)).includes("restart it where it runs"), await sub(p));
  for (const width of [420, 280]) {
    await p.setViewportSize({ width, height: 800 });
    const cut = await p.evaluate(() => { const el = document.getElementById("sc-sub"); return el.scrollWidth > el.clientWidth + 1; });
    const stop = await p.evaluate(() => { const r = document.getElementById("sc-stop").getBoundingClientRect(); return r.right <= window.innerWidth + 1 && r.width > 20; });
    check(`all of it is on the screen at ${width}px, and Stop is still there`, !cut && stop, { cut, stop });
  }
  await p.setViewportSize({ width: 420, height: 800 });
  await say(p, "파일 올렸어. 이어서 해줘");
  await pass(p, 2500);                      // the dot is looked at every two seconds
  check("a message typed at it does not say it is on its way", (await pending(p))[0]?.includes("not delivered — the session is not answering"), await pending(p));
  // The owner keeps using the page: every look below is inside a fresh mark's twenty seconds.
  let called = [];
  for (let i = 0; i < 12; i++) {
    await p.mouse.click(200, 300); await pass(p, 9000);
    called.push(await dot(p));
  }
  check("and no amount of typing at it makes it 'connected' again", called.every((c) => c === "stale"), called);
  check("the message is still marked undelivered", (await pending(p))[0]?.includes("not delivered"), await pending(p));

  // The session is started again: a new sid, a clean log, and — as its first
  // word — what the old one never read, handed over by the new session.
  await p.evaluate((h) => {
    window.__push("content", "log-s2-0000", { events: [
      { i: 0, kind: "status", text: "connected — this session is now driven from here as well as the terminal", t: 5 },
      { i: 1, kind: "unread", known: true, level: "warn", text: "1 message(s) typed here …",
        items: [{ text: "파일 올렸어. 이어서 해줘", at: Date.parse("2026-11-18T08:19:10Z") }], t: 6 }] });
    window.__push("content", "head", h);
  }, head({ sid: "s2", rev: 1, events: 2, beat: 77 }));
  await pass(p, 3000);
  check("the restarted session shows as connected", (await dot(p)) === "live", await sub(p));
  check("the old message is not shown as sent or sending", (await pending(p)).length === 0, await pending(p));
  const card = await p.evaluate(() => document.querySelector(".sc-unread")?.innerText || "");
  check("what was typed at the old session is on the page, said to be unread and not run",
    card.includes("파일 올렸어. 이어서 해줘") && card.includes("never read") && card.includes("Nothing below was run"), card);
  check("nothing is put in the box or sent by itself", (await p.inputValue("#sc-input")) === ""
    && await p.evaluate(() => Object.keys(window.__db.responses || {}).filter((k) => k.startsWith("inbox-")).length) === 1,
    await p.inputValue("#sc-input"));
  await p.click(".sc-unread .sc-btn");
  check("one press puts it back in the message box", (await p.inputValue("#sc-input")) === "파일 올렸어. 이어서 해줘", await p.inputValue("#sc-input"));
  await p.keyboard.press("Enter"); await pass(p, 500);
  const sent = await p.evaluate(() => Object.values(window.__db.responses).filter((d) => d.doc === "inbox" && d.sid === "s2").map((d) => d.text));
  check("and Enter sends it to the session that is there now", sent.length === 1 && sent[0] === "파일 올렸어. 이어서 해줘", sent);
  for (const width of [420, 280]) {
    await p.setViewportSize({ width, height: 800 });
    const over = await p.evaluate(() => { const el = document.querySelector(".sc-unread"); return el.scrollWidth > el.clientWidth + 1 || el.getBoundingClientRect().right > window.innerWidth + 1; });
    check(`the card fits at ${width}px`, !over, over);
  }
  await p.close();
}
// 1b. A page that does not know the kind (an older tab) still shows the words.
{
  const p = await open();
  await p.evaluate((h) => {
    window.__push("content", "log-s1-0000", { events: [
      { i: 0, kind: "unread-from-the-future", level: "warn", text: "1 message(s) typed here were never read\n• 이어서 해줘", t: 6 }] });
    window.__push("content", "head", h);
  }, head({ events: 1 }));
  await pass(p, 2000);
  check("an unknown kind is drawn as its text", (await p.evaluate(() => document.getElementById("sc-log").innerText)).includes("이어서 해줘"), "");
  await p.close();
}
// 2. A live session that is merely slow to read: the message keeps its ordinary wording.
{
  const p = await open();
  await p.evaluate((h) => window.__push("content", "head", h), head({}));
  await pass(p, 2000);
  await say(p, "hello");
  await pass(p, 1000);
  check("to a live session: sending…", (await pending(p))[0] === "you · web · sending…", await pending(p));
  let beat = 1;
  for (let i = 0; i < 8; i++) { beat++; await p.evaluate((h) => window.__push("content", "head", h), head({ rev: beat, beat })); await pass(p, 5000); }
  check("it beats, so it stays connected through forty seconds of marks", (await dot(p)) === "live", await sub(p));
  check("and the wording does not change", (await pending(p))[0] === "you · web · sending…", await pending(p));
  await p.close();
}
// 3. Not answering, then answering: the message's wording comes back.
{
  const p = await open();
  await p.evaluate((h) => window.__push("content", "head", h), head({}));
  await pass(p, 2000);
  await say(p, "still there?");
  await pass(p, 40000);
  check("forty seconds of nothing: not delivered", (await pending(p))[0]?.includes("not delivered"), await pending(p));
  await p.evaluate((h) => window.__push("content", "head", h), head({ rev: 2, beat: 2 }));
  await pass(p, 2500);
  check("it beats again: connected, and the message is sending again",
    (await dot(p)) === "live" && (await pending(p))[0] === "you · web · sending…", [await sub(p), await pending(p)]);
  await p.close();
}
await b.close();
console.log(failed ? `\n${failed} FAILED` : "\nall passed");
process.exit(failed ? 1 : 0);
