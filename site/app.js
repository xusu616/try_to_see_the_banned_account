"use strict";

const repository = "https://github.com/xusu616/try_to_see_the_banned_account";
const api = "https://api.github.com/repos/xusu616/try_to_see_the_banned_account";
const releaseUrl = `${repository}/releases/tag/v0.1.1`;
const publicSite = "https://xusu616.github.io/try_to_see_the_banned_account/";
// file:// cannot fetch adjacent JSON. Direct local previews use the same public
// snapshot as the published site; HTTP previews read their own generated files.
const profileRoot = new URL("profiles/", location.protocol === "file:" ? publicSite : new URL("./", location.href));
const requestMarker = "<!-- profile-card-request -->";
const resultMarker = "<!-- profile-card-result -->";
const loginPattern = /^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$/;
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const form = document.querySelector("#lookup-form");
const loginInput = document.querySelector("#github-login");
const resultSection = document.querySelector("#result-section");
const statusPanel = document.querySelector("#request-status");
const statusTitle = document.querySelector("#status-title");
const statusDetail = document.querySelector("#status-detail");
const submitLink = document.querySelector("#submit-link");
const issueLink = document.querySelector("#issue-link");
const shareButton = document.querySelector("#share-button");
const resumeButton = document.querySelector("#resume-button");
const shareStatus = document.querySelector("#share-status");
const cardResult = document.querySelector("#card-result");
const cardCount = document.querySelector("#card-count");
const galleryStatus = document.querySelector("#gallery-status");
const galleryRetry = document.querySelector("#gallery-retry");
const refreshButton = document.querySelector("#refresh-button");
const previousPage = document.querySelector("#previous-page");
const nextPage = document.querySelector("#next-page");
const pageSelect = document.querySelector("#page-select");
const pageSummary = document.querySelector("#page-summary");
const verifiedProfiles = new Map();
const galleryPages = new Map();
const excludedProfiles = new Set();
let currentProfile;
let profileIndex;
let indexPromise;
let cardPage = 1;
let galleryLoading = false;
let galleryError = "";
let galleryLoadId = 0;
let activeRequest;

class ApiError extends Error {
  constructor(status, retryAt = 0) {
    super(`GitHub API ${status}`);
    this.status = status;
    this.retryAt = retryAt;
  }
}

function normalizedLogin(value) {
  const login = value.trim().replace(/^@/, "");
  return login.length <= 39 && loginPattern.test(login) ? login : null;
}

function current(request) {
  return activeRequest === request && !request.controller.signal.aborted;
}

function showStatus(title, detail, state = "waiting") {
  resultSection.hidden = false;
  statusPanel.dataset.state = state;
  statusTitle.textContent = title;
  statusDetail.textContent = detail;
}

function resetRequest() {
  if (activeRequest) {
    activeRequest.controller.abort();
    clearTimeout(activeRequest.timer);
  }
  activeRequest = undefined;
  submitLink.hidden = true;
  issueLink.hidden = true;
  shareButton.hidden = true;
  resumeButton.hidden = true;
  refreshButton.hidden = true;
  shareStatus.hidden = true;
}

function makeRequest(login, requestId, issueNumber = null) {
  const startedAt = Date.now();
  return {
    login, requestId, issueNumber, startedAt,
    since: new Date(startedAt - 5000).toISOString(),
    deadline: startedAt + 10 * 60 * 1000,
    controller: new AbortController(),
    timer: null, retryAt: 0, polling: false,
  };
}

function issueFound(request, number) {
  request.issueNumber = number;
  issueLink.href = `${repository}/issues/${number}`;
  issueLink.hidden = false;
  shareButton.hidden = false;
  submitLink.hidden = true;
  history.replaceState(null, "", `?${new URLSearchParams({ request: String(number) })}`);
}

function parseIssue(issue) {
  if (!issue || issue.pull_request || typeof issue.body !== "string" || !issue.body.startsWith(requestMarker)) return null;
  const requestId = issue.body.match(/^Request ID: ([^\r\n]+)\r?$/m)?.[1];
  const login = issue.body.match(/^GitHub username: ([^\r\n]+)\r?$/m)?.[1];
  if (!requestId || !uuidPattern.test(requestId) || !login || normalizedLogin(login) !== login) return null;
  if (issue.title !== `Verify profile: ${login}` || !Number.isSafeInteger(issue.number) || issue.number < 1) return null;
  return { login, requestId };
}

function retryTime(response) {
  const retryAfter = response.headers.get("Retry-After");
  if (retryAfter) {
    const seconds = Number(retryAfter);
    const time = Number.isFinite(seconds) ? Date.now() + seconds * 1000 : Date.parse(retryAfter);
    if (Number.isFinite(time) && time > Date.now()) return time;
  }
  const reset = Number(response.headers.get("X-RateLimit-Reset"));
  return Number.isFinite(reset) && reset * 1000 > Date.now() ? reset * 1000 : 0;
}

async function requestJson(path, request) {
  return readJson(`${api}${path}`, request);
}

async function readJson(url, request, allowMissing = false) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (request.controller.signal.aborted) controller.abort();
  request.controller.signal.addEventListener("abort", abort, { once: true });
  const timeout = setTimeout(abort, 15000);
  try {
    const githubRequest = url.startsWith(api);
    const response = await fetch(url, {
      signal: controller.signal, cache: githubRequest ? "no-store" : "no-cache",
      headers: { Accept: githubRequest ? "application/vnd.github+json" : "application/json" },
    });
    if (allowMissing && response.status === 404) return null;
    if (!response.ok) throw new ApiError(response.status, retryTime(response));
    return await response.json();
  } finally {
    clearTimeout(timeout);
    request.controller.signal.removeEventListener("abort", abort);
  }
}

async function findIssue(request) {
  const parameters = new URLSearchParams({ state: "all", sort: "created", direction: "desc", per_page: "100", since: request.since });
  const issues = await requestJson(`/issues?${parameters}`, request);
  if (!Array.isArray(issues)) throw new Error("Invalid issue response");
  return issues.find((issue) => {
    const parsed = parseIssue(issue);
    return parsed?.requestId === request.requestId && parsed.login === request.login;
  });
}

function safeAvatar(value) {
  if (!value) return "";
  const address = new URL(value);
  if (address.origin !== "https://avatars.githubusercontent.com" || address.username || address.password || address.hash ||
      !/^\/(?:u|in)\/\d+$/.test(address.pathname) ||
      [...address.searchParams].some(([key, parameter]) => !["s", "v"].includes(key) || !/^\d+$/.test(parameter))) {
    throw new Error("Invalid avatar URL");
  }
  return address.href;
}

function parseResult(comment, request) {
  if (comment?.user?.login !== "github-actions[bot]" || comment.user.type !== "Bot" ||
      typeof comment.body !== "string" || !comment.body.startsWith(resultMarker)) return null;
  const fenced = comment.body.match(/```json\s*\n([\s\S]*?)\n```/);
  if (!fenced) return null;
  let result;
  try { result = JSON.parse(fenced[1]); } catch { return null; }
  if (!result || typeof result !== "object" || result.requestId !== request.requestId ||
      typeof result.login !== "string" || result.login.toLowerCase() !== request.login.toLowerCase()) return null;
  if (!["verified", "unavailable", "error", "excluded"].includes(result.status)) throw new Error("Invalid result status");
  if (result.status === "verified") validateProfileFields(result);
  if (result.status === "excluded") {
    validateProfileFields(result);
    if (!Number.isInteger(result.profilePageStatus) || result.profilePageStatus < 200 ||
        result.profilePageStatus >= 500 || [403, 404, 429].includes(result.profilePageStatus)) {
      throw new Error("Invalid exclusion status");
    }
  }
  return result;
}

function validateProfileFields(result) {
  if (result.releaseUrl !== releaseUrl || typeof result.name !== "string" || typeof result.bio !== "string" ||
        typeof result.avatarUrl !== "string" || typeof result.badge !== "string" ||
        !["", "PRO"].includes(result.badge) ||
        typeof result.observedAt !== "string" || !Number.isFinite(Date.parse(result.observedAt))) {
    throw new Error("Invalid profile result");
  }
  result.avatarUrl = safeAvatar(result.avatarUrl);
}

async function findResult(request) {
  // GitHub returns comments oldest first. A result may be beyond the first page.
  for (let page = 1; ; page += 1) {
    const comments = await requestJson(`/issues/${request.issueNumber}/comments?per_page=100&page=${page}`, request);
    if (!Array.isArray(comments)) throw new Error("Invalid comment response");
    for (const comment of comments) {
      const result = parseResult(comment, request);
      if (result) return result;
    }
    if (comments.length < 100 || !current(request)) return null;
  }
}

function profileBio(value) {
  // Some public bios contain the literal characters backslash + n instead of LF.
  return value.replace(/\\r\\n|\\n|\\r/g, "\n");
}

function renderCard(result) {
  const card = document.querySelector("#card-template").content.firstElementChild.cloneNode(true);
  card.querySelector(".profile-name").textContent = result.name || result.login;
  card.querySelector(".profile-login").textContent = `@${result.login}`;
  card.querySelector(".avatar-initial").textContent = result.login[0].toUpperCase();
  if (result.avatarUrl) {
    const image = card.querySelector("img");
    image.addEventListener("error", () => { image.hidden = true; }, { once: true });
    image.src = result.avatarUrl;
    image.hidden = false;
  }
  if (result.bio) {
    const bio = card.querySelector(".profile-bio");
    bio.textContent = profileBio(result.bio);
    bio.hidden = false;
  }
  if (result.badge) {
    const badge = card.querySelector(".profile-badge");
    badge.textContent = result.badge;
    badge.hidden = false;
  }
  const time = card.querySelector("time");
  time.dateTime = result.observedAt;
  time.textContent = `核验于 ${new Date(result.observedAt).toLocaleString("zh-CN", { hour12: false })}`;
  card.querySelector(".card-request-link").href = `?${new URLSearchParams({ request: String(result.issueNumber) })}`;
  return card;
}

function rememberVerified(result, issueNumber) {
  if (result.status !== "verified" || result.profilePageStatus !== 404) return;
  const key = result.login.toLowerCase();
  if (excludedProfiles.has(key)) return;
  const previous = verifiedProfiles.get(key);
  if (!previous || Date.parse(result.observedAt) >= Date.parse(previous.observedAt)) {
    verifiedProfiles.set(key, { ...result, issueNumber });
  }
}

function renderGallery() {
  const pageCount = profileIndex?.pageCount || 1;
  const profiles = (galleryPages.get(cardPage) || []).filter((profile) => !excludedProfiles.has(profile.login.toLowerCase()));
  cardResult.replaceChildren(...profiles.map((profile) => renderCard(verifiedProfiles.get(profile.login.toLowerCase()) || profile)));
  const currentCard = document.querySelector("#current-card");
  const showCurrent = currentProfile && !profiles.some((profile) => profile.login.toLowerCase() === currentProfile.login.toLowerCase());
  currentCard.replaceChildren(...(showCurrent ? [renderCard(currentProfile)] : []));
  cardCount.textContent = profileIndex ? `${excludedProfiles.size ? "列表更新中" : `${profileIndex.total} 个账号`} · 每页 ${profileIndex.pageSize} 个` : "";
  const options = Array.from({ length: pageCount }, (_, index) => {
    const option = document.createElement("option");
    option.value = String(index + 1);
    option.textContent = `第 ${index + 1} 页`;
    return option;
  });
  pageSelect.replaceChildren(...options);
  pageSelect.value = String(cardPage);
  pageSelect.disabled = pageCount === 1;
  previousPage.disabled = cardPage === 1;
  nextPage.disabled = cardPage === pageCount;
  pageSummary.textContent = `第 ${cardPage} / ${pageCount} 页`;
  galleryRetry.hidden = !galleryError;
  galleryRetry.disabled = galleryLoading;
  galleryStatus.hidden = !galleryLoading && !galleryError && !excludedProfiles.size && profiles.length > 0;
  if (galleryLoading) {
    galleryStatus.textContent = "正在读取已核验账号…";
  } else if (galleryError) {
    galleryStatus.textContent = galleryError;
  } else if (excludedProfiles.size) {
    galleryStatus.textContent = "不符合条件的账号已移除，账号数量和分页将在发布后更新。";
  } else {
    galleryStatus.textContent = profileIndex?.total ? "新核验结果已显示，账号列表将在发布后更新。" : "还没有主页返回 404 且资料卡核验成功的账号。可在顶栏发起查询。";
  }
}

async function loadProfileIndex() {
  if (profileIndex) return profileIndex;
  if (!indexPromise) {
    const request = { controller: new AbortController() };
    indexPromise = readJson(new URL("index.json", profileRoot).href, request).then((index) => {
      if (!index || !Number.isSafeInteger(index.total) || index.total < 0 ||
          !Number.isSafeInteger(index.pageSize) || index.pageSize < 1 ||
          index.pageCount !== Math.max(1, Math.ceil(index.total / index.pageSize))) {
        throw new Error("Invalid profile index");
      }
      profileIndex = index;
      return index;
    }).finally(() => { indexPromise = undefined; });
  }
  return indexPromise;
}

function validateIndexedProfile(profile) {
  if (!profile || profile.status !== "verified" || profile.profilePageStatus !== 404 || normalizedLogin(profile.login || "") !== profile.login ||
      !Number.isSafeInteger(profile.issueNumber) || profile.issueNumber < 1 ||
      !uuidPattern.test(profile.requestId || "")) throw new Error("Invalid indexed profile");
  // Snapshot files are published by the repository's trusted indexing workflow.
  validateProfileFields(profile);
  return profile;
}

async function loadGallery(page = cardPage) {
  const loadId = ++galleryLoadId;
  galleryLoading = true;
  galleryError = "";
  renderGallery();
  const request = { controller: new AbortController() };
  try {
    await loadProfileIndex();
    if (!galleryPages.has(page)) {
      const profiles = await readJson(new URL(`pages/${page}.json`, profileRoot).href, request);
      const expectedCount = Math.min(profileIndex.pageSize, profileIndex.total - (page - 1) * profileIndex.pageSize);
      if (!Array.isArray(profiles) || profiles.length !== expectedCount) throw new Error("Invalid profile page");
      const validated = profiles.map(validateIndexedProfile);
      for (const profile of validated) rememberVerified(profile, profile.issueNumber);
      galleryPages.set(page, validated);
    }
    if (loadId === galleryLoadId) cardPage = page;
  } catch (error) {
    if (loadId === galleryLoadId) {
      galleryError = error instanceof ApiError && error.status === 404
        ? "账号列表尚未发布，请稍后重新读取。"
        : "账号列表暂时无法读取，请重新读取。";
    }
  } finally {
    if (loadId === galleryLoadId) {
      galleryLoading = false;
      renderGallery();
    }
  }
}

async function changeCardPage(page) {
  await loadGallery(page);
  document.querySelector("#gallery-heading").scrollIntoView({ block: "start" });
}

function reloadGallery() {
  // A publication can change both the index and its pages. Retry the complete
  // snapshot rather than retaining metadata from the failed read.
  profileIndex = undefined;
  galleryPages.clear();
  verifiedProfiles.clear();
  if (currentProfile) rememberVerified(currentProfile, currentProfile.issueNumber);
  cardPage = 1;
  return loadGallery(1);
}

function complete(request, result) {
  clearTimeout(request.timer);
  resumeButton.hidden = true;
  if (result.status === "excluded") {
    excludedProfiles.add(result.login.toLowerCase());
    verifiedProfiles.delete(result.login.toLowerCase());
    if (currentProfile?.login.toLowerCase() === result.login.toLowerCase()) currentProfile = undefined;
    renderGallery();
    showStatus("该账号不符合收录条件", `@${result.login} · 主页返回 HTTP ${result.profilePageStatus}，英灵殿只收录主页返回 404 的账号。`, "unavailable");
    return;
  }
  if (result.status === "verified") {
    if (result.profilePageStatus !== 404) {
      refreshButton.hidden = false;
      showStatus("旧结果需要重新核验", "这条记录尚未确认主页是否返回 404，请点击“重新核验资料”。", "unavailable");
      return;
    }
    excludedProfiles.delete(result.login.toLowerCase());
    rememberVerified(result, request.issueNumber);
    // A result is immediately reviewable even while the next Pages snapshot is building.
    currentProfile = verifiedProfiles.get(result.login.toLowerCase());
    renderGallery();
    refreshButton.hidden = false;
    showStatus("已找到资料卡", `@${result.login} · 已核验 Release 原页，资料卡已加入下方账号列表。`, "success");
  } else if (result.status === "unavailable") {
    showStatus("本次未能在来源页确认资料卡", typeof result.message === "string" && result.message ? result.message : "来源页未返回此用户的可用资料卡；这不代表账号不存在。", "unavailable");
  } else {
    showStatus("本次核验未完成", typeof result.message === "string" && result.message ? result.message : "核验服务执行失败，可查看查询请求中的详情后重新查询。", "error");
  }
}

function pauseForError(request, error) {
  resumeButton.hidden = false;
  if (error instanceof ApiError && (error.status === 403 || error.status === 429)) {
    request.retryAt = error.retryAt;
    const when = error.retryAt ? `请在 ${new Date(error.retryAt).toLocaleTimeString("zh-CN", { hour12: false })} 后继续等待。` : "请稍后点击“继续等待”。";
    showStatus("GitHub 暂时限制了结果读取", `${when} 这不表示用户资料不可用。`, "error");
  } else if (error instanceof ApiError && error.status === 404) {
    showStatus("查询请求暂时无法读取", "请确认请求链接有效且公开，再点击“继续等待”。", "error");
  } else {
    showStatus("暂时无法读取查询结果", "网络或服务响应异常。可查看 GitHub 查询请求，恢复后点击“继续等待”。", "error");
  }
}

async function poll(request) {
  if (!current(request) || request.polling) return;
  if (Date.now() >= request.deadline) {
    showStatus("已等待 10 分钟", request.issueNumber ? "任务可能仍在排队。可查看查询请求，或点击“继续等待”。" : "尚未找到提交记录。请先在 GitHub 确认提交，再点击“继续等待”。", "error");
    resumeButton.hidden = false;
    return;
  }
  request.polling = true;
  try {
    if (request.issueNumber && !request.requestId) {
      const issue = await requestJson(`/issues/${request.issueNumber}`, request);
      if (!current(request)) return;
      const parsed = parseIssue(issue);
      if (!parsed) {
        showStatus("该链接不是有效的资料卡查询", "请输入 GitHub 用户名发起新的查询。", "error");
        return;
      }
      request.login = parsed.login;
      request.requestId = parsed.requestId;
      loginInput.value = parsed.login;
      issueFound(request, request.issueNumber);
    }
    if (!request.issueNumber) {
      const issue = await findIssue(request);
      if (!current(request)) return;
      if (issue) issueFound(request, issue.number);
    }
    if (request.issueNumber) {
      showStatus("已收到查询请求，正在实时核验", `@${request.login} · 可以留在此页，结果将自动显示。`);
      const result = await findResult(request);
      if (!current(request)) return;
      if (result) { complete(request, result); return; }
    }
    if (current(request)) {
      const interval = Date.now() - request.startedAt < 2 * 60 * 1000 ? 20000 : 30000;
      request.timer = setTimeout(() => poll(request), interval);
    }
  } catch (error) {
    if (current(request)) pauseForError(request, error);
  } finally {
    request.polling = false;
  }
}

function submitVerification(request) {
  const issueUrl = new URL(`${repository}/issues/new`);
  issueUrl.searchParams.set("title", `Verify profile: ${request.login}`);
  issueUrl.searchParams.set("body", `${requestMarker}\n\nRequest ID: ${request.requestId}\n\nGitHub username: ${request.login}`);
  submitLink.href = issueUrl.href;
  submitLink.hidden = false;
  history.replaceState(null, "", `?${new URLSearchParams({ login: request.login })}`);
  showStatus("尚未收录，请提交核验请求", `@${request.login} · 点击“打开 GitHub 提交页面”确认一次，返回此页即可等待结果。`);
  poll(request);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const login = normalizedLogin(loginInput.value);
  loginInput.setCustomValidity(login ? "" : "请输入有效的 GitHub 用户名：最多 39 个字母、数字或单个连字符。");
  if (!login) { loginInput.reportValidity(); return; }
  loginInput.value = login;
  resetRequest();
  const request = makeRequest(login, crypto.randomUUID());
  activeRequest = request;
  showStatus("正在查询已有资料", `@${login} · 优先读取已核验账号，无需重复提交。`);
  try {
    await loadProfileIndex();
    if (!current(request)) return;
    let profile = verifiedProfiles.get(login.toLowerCase());
    if (!profile && !excludedProfiles.has(login.toLowerCase())) {
      const indexed = await readJson(new URL(`users/${login.toLowerCase()}.json`, profileRoot).href, request, true);
      if (indexed) profile = validateIndexedProfile(indexed);
    }
    if (!current(request)) return;
    if (profile) {
      request.requestId = profile.requestId;
      request.login = profile.login;
      issueFound(request, profile.issueNumber);
      complete(request, profile);
      showStatus("已找到已核验资料", `@${profile.login} · 资料卡已显示在下方，可按需重新核验。`, "success");
      return;
    }
    submitVerification(request);
  } catch (error) {
    if (!current(request)) return;
    showStatus("暂时无法读取已有资料", "读取失败，请稍后再次查询；本次尚未创建核验请求。", "error");
  }
});

loginInput.addEventListener("input", () => loginInput.setCustomValidity(""));
previousPage.addEventListener("click", () => changeCardPage(cardPage - 1));
nextPage.addEventListener("click", () => changeCardPage(cardPage + 1));
pageSelect.addEventListener("change", () => changeCardPage(Number(pageSelect.value)));
galleryRetry.addEventListener("click", reloadGallery);
refreshButton.addEventListener("click", () => {
  const login = activeRequest.login;
  resetRequest();
  const request = makeRequest(login, crypto.randomUUID());
  activeRequest = request;
  submitVerification(request);
});

resumeButton.addEventListener("click", () => {
  const request = activeRequest;
  if (!request || request.polling) return;
  if (request.retryAt > Date.now()) {
    showStatus("请等待 GitHub 恢复读取额度", `可在 ${new Date(request.retryAt).toLocaleTimeString("zh-CN", { hour12: false })} 后继续等待。`, "error");
    return;
  }
  request.deadline = Date.now() + 10 * 60 * 1000;
  resumeButton.hidden = true;
  showStatus("正在继续等待", request.issueNumber ? "正在读取核验结果。" : "正在查找已提交的 GitHub 查询请求。");
  poll(request);
});

shareButton.addEventListener("click", async () => {
  const request = activeRequest;
  if (!request?.issueNumber) return;
  const url = new URL(location.pathname, location.origin);
  url.searchParams.set("request", String(request.issueNumber));
  try {
    await navigator.clipboard.writeText(url.href);
    if (!current(request)) return;
    shareStatus.textContent = "结果链接已复制，可直接打开查看本次核验。";
  } catch {
    if (!current(request)) return;
    shareStatus.textContent = `请复制此链接：${url.href}`;
  }
  shareStatus.hidden = false;
});

async function initialize() {
  const parameters = new URLSearchParams(location.search);
  const login = normalizedLogin(parameters.get("login") || "");
  if (login) loginInput.value = login;
  const issue = parameters.get("request");
  if (issue !== null) {
    if (!/^[1-9]\d*$/.test(issue) || !Number.isSafeInteger(Number(issue))) {
      showStatus("查询链接无效", "请使用完整的结果分享链接，或输入用户名重新查询。", "error");
      return;
    }
    const request = makeRequest("", "", Number(issue));
    activeRequest = request;
    showStatus("正在读取查询请求", "正在从 GitHub 获取本次请求与核验结果。");
    poll(request);
    return;
  }
  const legacyCard = parameters.get("card");
  if (legacyCard && !login) {
    try {
      const response = await fetch("./cards.json");
      if (!response.ok) throw new Error("Legacy cards unavailable");
      const cards = await response.json();
      if (activeRequest) return;
      const card = Array.isArray(cards) ? cards.find((item) => item.id === legacyCard) : null;
      const legacyLogin = normalizedLogin(card?.login || "");
      if (legacyLogin) loginInput.value = legacyLogin;
      showStatus("这是旧版资料卡链接", legacyLogin ? "已填入用户名，点击“查询资料卡”进行本次实时核验。" : "请输入 GitHub 用户名进行本次实时核验。", "unavailable");
    } catch {
      if (!activeRequest) showStatus("旧版资料卡链接无法读取", "请输入 GitHub 用户名重新查询。", "error");
    }
  }
}

loadGallery();
initialize();
