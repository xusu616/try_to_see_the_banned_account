const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

const source = readFileSync(resolve(__dirname, "../site/app.js"), "utf8")
  .replace(/loadGallery\(\);\s*initialize\(\);\s*$/, "");
const repository = "https://github.com/xusu616/try_to_see_the_banned_account";
const requestId = "abcdef12-1234-4234-8234-123456789abc";
const snapshotId = "11111111-1111-4111-8111-111111111111";
const nextSnapshotId = "22222222-2222-4222-8222-222222222222";

class Element {
  constructor() {
    this.hidden = true;
    this.disabled = false;
    this.value = "";
    this.textContent = "";
    this.dataset = {};
    this.children = [];
    this.elements = new Map();
    this.listeners = new Map();
  }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  async emit(type, event = {}) {
    for (const handler of this.listeners.get(type) || []) await handler({ preventDefault() {}, ...event });
  }
  querySelector(selector) {
    if (!this.elements.has(selector)) this.elements.set(selector, new Element());
    return this.elements.get(selector);
  }
  cloneNode() { return new Element(); }
  replaceChildren(...children) { this.children = children; }
  setCustomValidity(value) { this.validationMessage = value; }
  reportValidity() {}
  scrollIntoView() {}
  requestSubmit() { return this.emit("submit"); }
}

function profile(login, overrides = {}) {
  return {
    login, name: login, bio: "first\nsecond", avatarUrl: "", badge: "",
    status: "verified", profilePageStatus: 404, issueNumber: 5, requestId, snapshotId,
    observedAt: "2026-11-19T00:00:00Z", releaseUrl: `${repository}/releases/tag/v0.1.1`,
    ...overrides,
  };
}

function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

function resultComment(result) {
  return { user: { login: "github-actions[bot]", type: "Bot" }, body: `<!-- profile-card-result -->\n\`\`\`json\n${JSON.stringify(result)}\n\`\`\`` };
}

function issue(login, number, id = requestId) {
  return { number, title: `Verify profile: ${login}`, body: `<!-- profile-card-request -->\n\nRequest ID: ${id}\n\nGitHub username: ${login}` };
}

function createApp(fetcher) {
  const document = new Element();
  document.createElement = () => new Element();
  document.querySelector("#card-template").content = { firstElementChild: new Element() };
  const calls = [];
  const timers = new Map();
  let timerId = 0;
  const location = { protocol: "https:", href: "https://example.test/", search: "" };
  const context = {
    document, console, URL, URLSearchParams, AbortController, Date, crypto: webcrypto, location,
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    history: { replaceState(_state, _title, url) { const next = new URL(url, location.href); location.href = next.href; location.search = next.search; } },
    fetch: async (url, options) => {
      calls.push(String(url));
      return fetcher(new URL(url), options);
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  const run = (expression) => vm.runInContext(expression, context);
  const element = (selector) => document.querySelector(selector);
  const query = async (login) => { element("#github-login").value = login; await element("#lookup-form").emit("submit"); };
  const start = (login, number = 5) => {
    context.testRequest = { login, number, requestId };
    run("resetRequest(); activeRequest = makeRequest(testRequest.login, testRequest.requestId, testRequest.number);");
  };
  return { calls, context, timers, run, element, query, start };
}

function snapshotResponse(url, profiles, revision = snapshotId) {
  profiles = profiles.map((item) => ({ ...item, snapshotId: revision }));
  const total = profiles.length;
  if (url.pathname === "/profiles/index.json") return json({ total, pageSize: 6, pageCount: Math.max(1, Math.ceil(total / 6)), snapshotId: revision });
  const page = url.pathname.match(/^\/profiles\/pages\/(\d+)\.json$/);
  if (page) return json(profiles.slice((Number(page[1]) - 1) * 6, Number(page[1]) * 6));
  const user = url.pathname.match(/^\/profiles\/users\/(.+)\.json$/);
  if (user) {
    const found = profiles.find((item) => item.login.toLowerCase() === user[1]);
    return found ? json(found) : json({}, 404);
  }
  throw new Error(`Unexpected request: ${url}`);
}

function displayed(app) {
  return ["#recent-cards", "#card-result"].flatMap((selector) => app.element(selector).children)
    .map((card) => card.querySelector(".profile-login").textContent);
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("a stored user is read without any GitHub API request or full-index scan", async () => {
  const profiles = [...Array.from({ length: 6 }, (_, index) => profile(`first${index}`)), profile("stored")];
  const app = createApp((url) => url.hostname === "api.github.com" ? json({}, 403) : snapshotResponse(url, profiles));
  await app.run("loadGallery()");
  app.calls.length = 0;
  await app.query("stored");
  assert.deepEqual(app.calls, ["https://example.test/profiles/users/stored.json"]);
  assert.equal(app.element("#status-title").textContent, "已找到资料卡");
  assert.equal(app.element("#current-card").children.length, 1);
  assert.equal(app.element("#submit-link").hidden, true);
});

test("only an explicit static 404 proceeds to the public API and cached public users stay usable offline", async () => {
  let offline = false;
  const app = createApp((url) => {
    if (offline) throw new Error("offline");
    if (url.hostname === "api.github.com") return json({ login: "public", html_url: "https://github.com/public", name: "Public", bio: "bio", avatar_url: "" });
    return snapshotResponse(url, []);
  });
  await app.query("public");
  assert.deepEqual(app.calls, ["https://example.test/profiles/users/public.json", "https://api.github.com/users/public"]);
  assert.equal(app.element("#status-title").textContent, "已找到公开资料");
  assert.deepEqual(displayed(app), []);
  offline = true;
  await app.query("public");
  assert.equal(app.calls.length, 2);
  assert.equal(app.element("#status-title").textContent, "已找到公开资料");
});

test("static errors never become a missing-user response or offer an unnecessary submission", async () => {
  for (const status of [403, 500]) {
    const app = createApp(() => json({}, status));
    await app.query("stored");
    assert.equal(app.calls.length, 1);
    assert.equal(app.element("#status-title").textContent, "暂时无法读取资料");
    assert.equal(app.element("#submit-link").hidden, true);
  }
});

test("a later query owns the card even if an earlier static response completes after cancellation", async () => {
  let finishOld;
  const app = createApp((url) => url.pathname.endsWith("/old.json") ? new Promise((resolve) => { finishOld = resolve; }) : snapshotResponse(url, [profile("new")]));
  const oldQuery = app.query("old");
  await app.query("new");
  finishOld(json(profile("old")));
  await oldQuery;
  assert.equal(app.element("#current-card").children[0].querySelector(".profile-login").textContent, "@new");
  assert.equal(app.run("verifiedProfiles.has('old')"), false);
  assert.equal(app.element("#submit-link").hidden, true);
});

test("submission can be checked without clicking its link and does not poll before that choice", async () => {
  let foundRequest;
  const app = createApp((url) => {
    if (url.pathname.endsWith("/comments")) return json([resultComment({ ...profile("missing"), requestId: foundRequest.requestId, status: "unavailable", message: "No card returned." })]);
    if (url.pathname.endsWith("/issues")) return json([issue("missing", 12, foundRequest.requestId)]);
    return json({}, 404);
  });
  await app.query("missing");
  foundRequest = app.run("activeRequest");
  assert.equal(app.element("#submit-link").hidden, false);
  assert.equal(app.element("#resume-button").hidden, false);
  assert.equal(app.run("activeRequest.timer"), null);
  assert.equal(app.calls.length, 2);
  // A middle click or context-menu navigation does not dispatch the link's click event.
  await app.element("#resume-button").emit("click");
  await settle();
  assert.equal(app.element("#status-title").textContent, "本次未能在来源页确认资料卡");
  assert.equal(app.element("#issue-link").href, `${repository}/issues/12`);
});

test("a normal submission-link click still starts polling automatically", async () => {
  const app = createApp((url) => url.pathname.endsWith("/issues") ? json([]) : json({}, 404));
  await app.query("missing");
  await app.element("#submit-link").emit("click");
  await settle();
  assert.equal(app.calls.filter((url) => new URL(url).pathname.endsWith("/issues")).length, 1);
  assert.equal(app.element("#resume-button").hidden, false);
  assert.notEqual(app.run("activeRequest.timer"), null);
});

test("opening an old Issue for an unloaded second-page user does not add a pending page", async () => {
  const profiles = Array.from({ length: 12 }, (_, index) => profile(`user${index}`));
  const app = createApp((url) => url.pathname.endsWith("/comments") ? json([resultComment(profiles[6])]) : snapshotResponse(url, profiles));
  await app.run("loadGallery()");
  app.start("user6");
  await app.run("poll(activeRequest)");
  assert.deepEqual(displayed(app), profiles.slice(0, 6).map((item) => `@${item.login}`));
  assert.equal(app.element("#page-summary").textContent, "第 1 / 2 页");
  assert.equal(app.run("galleryLayout().pending.length"), 0);
  assert.ok(app.calls.includes("https://example.test/profiles/users/user6.json"));
});

test("an already loaded static page proves publication without an additional user-file request", async () => {
  const profiles = Array.from({ length: 12 }, (_, index) => profile(`user${index}`));
  const app = createApp((url) => url.pathname.endsWith("/comments") ? json([resultComment(profiles[6])]) : snapshotResponse(url, profiles));
  await app.run("loadGallery()");
  await app.run("changeCardPage(2)");
  await app.run("changeCardPage(1)");
  app.start("user6");
  await app.run("poll(activeRequest)");
  assert.equal(app.calls.some((url) => url.includes("/profiles/users/")), false);
  assert.equal(displayed(app).length, 6);
});

test("seven genuinely unpublished results occupy two pending pages followed by the static pages", async () => {
  const profiles = Array.from({ length: 12 }, (_, index) => profile(`stored${index}`));
  const app = createApp((url) => snapshotResponse(url, profiles));
  await app.run("loadGallery()");
  for (let index = 0; index < 7; index++) {
    app.start(`pending${index}`);
    app.context.result = profile(`pending${index}`);
    await app.run("complete(activeRequest, result)");
    assert.ok(displayed(app).length <= 6);
  }
  assert.deepEqual(displayed(app), ["@pending6"]);
  assert.equal(app.element("#page-summary").textContent, "第 2 / 4 页");
  await app.run("changeCardPage(1)");
  assert.deepEqual(displayed(app), Array.from({ length: 6 }, (_, index) => `@pending${index}`));
  await app.run("changeCardPage(3)");
  assert.deepEqual(displayed(app), profiles.slice(0, 6).map((item) => `@${item.login}`));
  await app.run("changeCardPage(4)");
  assert.deepEqual(displayed(app), profiles.slice(6).map((item) => `@${item.login}`));
  assert.equal(app.calls.filter((url) => url.includes("/profiles/pages/")).length, 2);
});

test("a later static-page read removes pending membership while keeping newer data and its Issue link", async () => {
  let published = false;
  const newer = profile("fresh", { observedAt: "2026-11-19T02:00:00Z", issueNumber: 42, name: "Newest" });
  const older = profile("fresh", { issueNumber: 7 });
  const app = createApp((url) => snapshotResponse(url, published ? [older] : [], published ? nextSnapshotId : snapshotId));
  await app.run("loadGallery()");
  app.start("fresh", 42);
  app.context.result = newer;
  await app.run("complete(activeRequest, result)");
  assert.deepEqual(displayed(app), ["@fresh"]);
  published = true;
  await app.run("reloadGallery()");
  assert.equal(app.run("galleryLayout().pending.length"), 0);
  assert.equal(app.run("verifiedProfiles.get('fresh').name"), "Newest");
  assert.equal(app.run("verifiedProfiles.get('fresh').issueNumber"), 42);
  assert.equal(displayed(app).length, 1);
});

test("an older Issue cannot replace a newer cached card or change its source Issue", async () => {
  const latest = profile("known", { observedAt: "2026-11-19T02:00:00Z", issueNumber: 42, name: "Newest" });
  const app = createApp((url) => snapshotResponse(url, [latest]));
  await app.run("loadGallery()");
  app.start("known", 7);
  app.context.result = profile("known", { issueNumber: 7 });
  await app.run("complete(activeRequest, result)");
  assert.equal(app.element("#current-card").children[0].querySelector(".profile-name").textContent, "Newest");
  assert.equal(app.element("#current-card").children[0].querySelector(".card-request-link").href, `${repository}/issues/42`);
  assert.equal(app.run("verifiedProfiles.get('known').issueNumber"), 42);
});

test("publication-read failure preserves the successful card without inventing pending membership, and retry recovers", async () => {
  let failing = true;
  const app = createApp((url) => url.pathname.includes("/profiles/users/") && failing ? json({}, 503) : snapshotResponse(url, []));
  await app.run("loadGallery()");
  app.start("fresh");
  app.context.result = profile("fresh");
  await app.run("complete(activeRequest, result)");
  assert.equal(app.element("#status-title").textContent, "已找到资料卡");
  assert.equal(app.element("#current-card").children.length, 1);
  assert.equal(app.element("#gallery-retry").hidden, false);
  assert.match(app.element("#gallery-status").textContent, /无法确认/);
  assert.equal(app.run("galleryLayout().pending.length"), 0);
  failing = false;
  await app.run("reloadGallery()");
  assert.equal(app.element("#gallery-retry").hidden, true);
  assert.deepEqual(displayed(app), ["@fresh"]);
});

test("a cancelled publication lookup cannot add its old account or overwrite the new card", async () => {
  let finishOld;
  const app = createApp((url) => url.pathname.endsWith("/old.json") ? new Promise((resolve) => { finishOld = resolve; }) : snapshotResponse(url, [profile("new")]));
  app.start("old");
  app.context.result = profile("old");
  const oldCompletion = app.run("complete(activeRequest, result)");
  await app.query("new");
  finishOld(json({}, 404));
  await oldCompletion;
  assert.equal(app.run("verifiedProfiles.has('old')"), false);
  assert.equal(app.element("#current-card").children[0].querySelector(".profile-login").textContent, "@new");
  assert.equal(app.run("galleryLayout().pending.length"), 0);
});

test("gallery metadata loading after a new result preserves the selected pending page", async () => {
  const profiles = Array.from({ length: 12 }, (_, index) => profile(`stored${index}`));
  let finishIndex;
  const app = createApp((url) => url.pathname.endsWith("/index.json") ? new Promise((resolve) => { finishIndex = resolve; }) : snapshotResponse(url, profiles));
  const initialLoad = app.run("loadGallery()");
  app.start("fresh");
  app.context.result = profile("fresh");
  await app.run("complete(activeRequest, result)");
  assert.equal(app.element("#page-summary").textContent, "第 1 / 2 页");
  finishIndex(json({ total: 12, pageSize: 6, pageCount: 2, snapshotId }));
  await initialLoad;
  assert.equal(app.element("#page-summary").textContent, "第 1 / 3 页");
  assert.deepEqual(displayed(app), ["@fresh"]);
});

test("an uppercase v4 request UUID matches the backend's lowercase result", async () => {
  const result = profile("known");
  const app = createApp((url) => {
    if (url.pathname.endsWith("/issues/5")) return json(issue("known", 5, requestId.toUpperCase()));
    if (url.pathname.endsWith("/comments")) return json([resultComment(result)]);
    return snapshotResponse(url, [result]);
  });
  app.run("activeRequest = makeRequest('', '', 5)");
  await app.run("poll(activeRequest)");
  assert.equal(app.element("#status-title").textContent, "已找到资料卡");
  assert.equal(app.run("activeRequest.requestId"), requestId);
});

test("a newly published user refreshes an earlier empty snapshot and appears in the gallery", async () => {
  let published = false;
  const result = profile("fresh");
  const app = createApp((url) => snapshotResponse(url, published ? [result] : [], published ? nextSnapshotId : snapshotId));
  await app.run("loadGallery()");
  assert.deepEqual(displayed(app), []);
  published = true;
  await app.query("fresh");
  assert.deepEqual(displayed(app), ["@fresh"]);
  assert.equal(app.run("profileIndex.snapshotId"), nextSnapshotId);
  assert.equal(app.run("galleryLayout().pending.length"), 0);
  assert.equal(app.calls.filter((url) => url.endsWith("/profiles/index.json")).length, 2);
  assert.equal(app.calls.filter((url) => url.endsWith("/profiles/pages/1.json")).length, 2);
});

test("snapshot identity detects newly published membership even when the total does not change", async () => {
  let published = false;
  const app = createApp((url) => snapshotResponse(url, [profile(published ? "fresh" : "removed")], published ? nextSnapshotId : snapshotId));
  await app.run("loadGallery()");
  assert.deepEqual(displayed(app), ["@removed"]);
  published = true;
  await app.query("fresh");
  assert.deepEqual(displayed(app), ["@fresh"]);
  assert.equal(app.run("profileIndex.total"), 1);
  assert.equal(app.run("profileIndex.snapshotId"), nextSnapshotId);
  assert.equal(app.run("galleryLayout().pending.length"), 0);
});

test("a pending user's successful publication removes its extra page only after synchronizing the snapshot", async () => {
  let published = false;
  const stored = Array.from({ length: 6 }, (_, index) => profile(`stored${index}`));
  const app = createApp((url) => snapshotResponse(url, published ? [profile("fresh"), ...stored] : stored, published ? nextSnapshotId : snapshotId));
  await app.run("loadGallery()");
  app.start("fresh");
  app.context.result = profile("fresh");
  await app.run("complete(activeRequest, result)");
  assert.equal(app.run("galleryLayout().pending.length"), 1);
  published = true;
  await app.query("fresh");
  assert.equal(app.run("galleryLayout().pending.length"), 0);
  assert.equal(app.run("profileIndex.snapshotId"), nextSnapshotId);
  assert.equal(displayed(app)[0], "@fresh");
  assert.equal(displayed(app).length, 6);
});

test("a page from a different publication cannot populate the current snapshot", async () => {
  const app = createApp((url) => url.pathname.endsWith("/index.json")
    ? json({ total: 1, pageSize: 6, pageCount: 1, snapshotId })
    : json([profile("wrong", { snapshotId: nextSnapshotId })]));
  await app.run("loadGallery()");
  assert.deepEqual(displayed(app), []);
  assert.equal(app.run("publishedLogins.size"), 0);
  assert.equal(app.element("#gallery-retry").hidden, false);
});

test("an old page response completing after refresh cannot overwrite the new snapshot", async () => {
  let next = false;
  let finishOldPage;
  const app = createApp((url) => {
    if (!next && url.pathname.endsWith("/pages/1.json")) return new Promise((resolve) => { finishOldPage = resolve; });
    return snapshotResponse(url, [profile(next ? "new" : "old")], next ? nextSnapshotId : snapshotId);
  });
  const oldLoad = app.run("loadGallery()");
  await settle();
  next = true;
  await app.run("reloadGallery()");
  finishOldPage(json([profile("old")]));
  await oldLoad;
  assert.equal(app.run("profileIndex.snapshotId"), nextSnapshotId);
  assert.deepEqual(displayed(app), ["@new"]);
  assert.equal(app.run("publishedLogins.has('old')"), false);
});

test("an old index response completing after refresh cannot replace the new index", async () => {
  let next = false;
  let finishOldIndex;
  const app = createApp((url) => {
    if (!next && url.pathname.endsWith("/index.json")) return new Promise((resolve) => { finishOldIndex = resolve; });
    return snapshotResponse(url, [profile("new")], nextSnapshotId);
  });
  const oldLoad = app.run("loadGallery()");
  next = true;
  await app.run("reloadGallery()");
  finishOldIndex(json({ total: 0, pageSize: 6, pageCount: 1, snapshotId }));
  await oldLoad;
  assert.equal(app.run("profileIndex.snapshotId"), nextSnapshotId);
  assert.deepEqual(displayed(app), ["@new"]);
  assert.equal(app.calls.filter((url) => url.includes("/profiles/pages/")).length, 1);
});

test("an unsynchronized publication preserves the successful card and offers reload without repeated fetches", async () => {
  const app = createApp((url) => url.pathname.includes("/profiles/users/")
    ? json(profile("fresh", { snapshotId: nextSnapshotId }))
    : snapshotResponse(url, []));
  await app.run("loadGallery()");
  app.start("fresh");
  app.context.result = profile("fresh");
  await app.run("complete(activeRequest, result)");
  assert.equal(app.element("#status-title").textContent, "已找到资料卡");
  assert.equal(app.element("#current-card").children.length, 1);
  assert.equal(app.run("galleryLayout().pending.length"), 0);
  assert.equal(app.element("#gallery-retry").hidden, false);
  assert.equal(app.calls.filter((url) => url.endsWith("/profiles/index.json")).length, 2);
  assert.equal(app.calls.filter((url) => url.includes("/profiles/users/")).length, 1);
});

test("legacy files without a snapshot ID trigger one refresh and never a retry loop", async () => {
  const legacy = profile("legacy");
  delete legacy.snapshotId;
  const app = createApp((url) => {
    if (url.pathname.endsWith("/index.json")) return json({ total: 1, pageSize: 6, pageCount: 1 });
    if (url.pathname.includes("/pages/")) return json([legacy]);
    if (url.pathname.includes("/users/")) return json(legacy);
    throw new Error(`Unexpected request: ${url}`);
  });
  await app.query("legacy");
  assert.equal(app.element("#status-title").textContent, "已找到资料卡");
  assert.equal(app.element("#gallery-retry").hidden, true);
  assert.deepEqual(displayed(app), ["@legacy"]);
  assert.equal(app.calls.filter((url) => url.endsWith("/profiles/index.json")).length, 2);
});

test("a new snapshot with fewer pages restores page one without requesting the removed page", async () => {
  let next = false;
  const oldProfiles = Array.from({ length: 18 }, (_, index) => profile(`old${index}`));
  const newProfiles = [profile("fresh"), ...oldProfiles.slice(0, 11)];
  const app = createApp((url) => snapshotResponse(url, next ? newProfiles : oldProfiles, next ? nextSnapshotId : snapshotId));
  await app.run("loadGallery()");
  await app.run("changeCardPage(3)");
  assert.equal(app.element("#page-summary").textContent, "第 3 / 3 页");
  next = true;
  app.calls.length = 0;
  await app.query("fresh");
  assert.equal(app.element("#page-summary").textContent, "第 1 / 2 页");
  assert.equal(displayed(app)[0], "@fresh");
  assert.equal(app.calls.some((url) => url.endsWith("/pages/3.json")), false);
  assert.equal(app.element("#gallery-retry").hidden, true);
});

test("failed metadata refresh does not invent a page count for the previously selected third page", async () => {
  let failing = false;
  const profiles = Array.from({ length: 18 }, (_, index) => profile(`stored${index}`));
  const app = createApp((url) => {
    if (failing && url.pathname.endsWith("/index.json")) return json({}, 503);
    if (url.pathname.endsWith("/users/fresh.json")) return json(profile("fresh", { snapshotId: nextSnapshotId }));
    return snapshotResponse(url, profiles);
  });
  await app.run("loadGallery()");
  await app.run("changeCardPage(3)");
  failing = true;
  await app.query("fresh");
  assert.equal(app.element("#status-title").textContent, "已找到资料卡");
  assert.equal(app.element("#page-summary").textContent, "页码暂不可用");
  assert.equal(app.element("#page-select").disabled, true);
  assert.equal(app.element("#previous-page").disabled, true);
  assert.equal(app.element("#next-page").disabled, true);
  assert.equal(app.element("#gallery-retry").hidden, false);
});
