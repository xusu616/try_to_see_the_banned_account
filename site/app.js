"use strict";

const repository = "https://github.com/xusu616/try_to_see_the_banned_account";
const api = "https://api.github.com/repos/xusu616/try_to_see_the_banned_account";
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
const resumeButton = document.querySelector("#resume-button");
const submitPrivacy = document.querySelector("#submit-privacy");
const currentCard = document.querySelector("#current-card");
const cardResult = document.querySelector("#card-result");
const galleryStatus = document.querySelector("#gallery-status");
const galleryRetry = document.querySelector("#gallery-retry");
const previousPage = document.querySelector("#previous-page");
const nextPage = document.querySelector("#next-page");
const pageSelect = document.querySelector("#page-select");
const pageSummary = document.querySelector("#page-summary");
const verifiedProfiles = new Map();
const galleryPages = new Map();
const publishedLogins = new Set();
const excludedProfiles = new Set();
const publicProfiles = new Map();
const galleryPageSize = 6;
let currentProfile;
let profileIndex;
let indexPromise;
let galleryView = { source: "published", page: 1 };
let galleryLoading = false;
let galleryError = "";
let publicationError = "";
let galleryLoadId = 0;
let gallerySnapshotId = 0;
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
  resumeButton.hidden = true;
  resumeButton.textContent = "检查提交结果";
  submitPrivacy.hidden = true;
  currentProfile = undefined;
  currentCard.replaceChildren();
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
  submitLink.hidden = true;
  submitPrivacy.hidden = true;
  resumeButton.textContent = "继续等待核验结果";
  history.replaceState(null, "", `?${new URLSearchParams({ request: String(number) })}`);
}

function parseIssue(issue) {
  if (!issue || issue.pull_request || typeof issue.body !== "string" || !issue.body.startsWith(requestMarker)) return null;
  const requestId = issue.body.match(/^Request ID: ([^\r\n]+)\r?$/m)?.[1];
  const login = issue.body.match(/^GitHub username: ([^\r\n]+)\r?$/m)?.[1];
  if (!requestId || !uuidPattern.test(requestId) || !login || normalizedLogin(login) !== login) return null;
  if (issue.title !== `Verify profile: ${login}` || !Number.isSafeInteger(issue.number) || issue.number < 1) return null;
  return { login, requestId: requestId.toLowerCase() };
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
    const githubRequest = url.startsWith("https://api.github.com/");
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

function validReleaseUrl(value) {
  if (typeof value !== "string" || !value.startsWith(`${repository}/releases/tag/`)) return false;
  return /^v0\.1\.[1-9]\d*$/.test(value.slice(`${repository}/releases/tag/`.length));
}

function validateProfileFields(result) {
  if (!validReleaseUrl(result.releaseUrl) || typeof result.name !== "string" || typeof result.bio !== "string" ||
        typeof result.avatarUrl !== "string" || typeof result.badge !== "string" ||
        !["", "PRO"].includes(result.badge) ||
        typeof result.observedAt !== "string" || !Number.isFinite(Date.parse(result.observedAt))) {
    throw new Error("Invalid profile result");
  }
  result.avatarUrl = safeAvatar(result.avatarUrl);
}

async function findResult(request) {
  // GitHub returns comments oldest first. A result may be beyond the first page.
  let latest = null;
  for (let page = 1; ; page += 1) {
    const comments = await requestJson(`/issues/${request.issueNumber}/comments?per_page=100&page=${page}`, request);
    if (!Array.isArray(comments)) throw new Error("Invalid comment response");
    for (const comment of comments) {
      const result = parseResult(comment, request);
      if (result && (!latest || Date.parse(result.observedAt) >= Date.parse(latest.observedAt))) latest = result;
    }
    if (comments.length < 100 || !current(request)) return latest;
  }
}

function profileBio(value) {
  // Some public bios contain the literal characters backslash + n instead of LF.
  return value.replace(/\\r\\n|\\n|\\r/g, "\n");
}

async function lookupPublicProfile(request) {
  const user = await readJson(`https://api.github.com/users/${request.login}`, request, true);
  if (!user) return null;
  if (typeof user.login !== "string" || normalizedLogin(user.login) !== user.login ||
      user.html_url !== `https://github.com/${user.login}` ||
      (user.name !== null && typeof user.name !== "string") ||
      (user.bio !== null && typeof user.bio !== "string")) throw new Error("Invalid public profile");
  const profile = {
    login: user.login, name: user.name || user.login, bio: user.bio || "",
    avatarUrl: safeAvatar(user.avatar_url), badge: "", status: "public",
    observedAt: new Date().toISOString(),
  };
  publicProfiles.set(request.login.toLowerCase(), profile);
  return profile;
}

function showPublicProfile(profile) {
  currentProfile = profile;
  currentCard.replaceChildren(renderCard(profile));
  showStatus("已找到公开资料", `@${profile.login} · 资料卡显示在这里，正常可见账号不加入英灵殿。`, "success");
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
  time.textContent = `${result.status === "public" ? "查询于" : "核验于"} ${new Date(result.observedAt).toLocaleString("zh-CN", { hour12: false })}`;
  const source = card.querySelector(".profile-source-link");
  source.href = result.status === "public" ? `https://github.com/${result.login}` : result.releaseUrl;
  source.textContent = result.status === "public" ? "查看 GitHub 主页" : "查看 Release 来源";
  const record = card.querySelector(".card-request-link");
  if (result.issueNumber) {
    record.href = `${repository}/issues/${result.issueNumber}`;
    record.hidden = false;
  }
  return card;
}

function rememberVerified(result, issueNumber) {
  if (result.status !== "verified" || result.profilePageStatus !== 404) return;
  const key = result.login.toLowerCase();
  if (excludedProfiles.has(key)) return;
  const previous = verifiedProfiles.get(key);
  if (!previous || Date.parse(result.observedAt) >= Date.parse(previous.observedAt)) {
    verifiedProfiles.set(key, { ...result, issueNumber, pendingPublication: result.pendingPublication === true && !publishedLogins.has(key) });
  } else if (publishedLogins.has(key)) {
    previous.pendingPublication = false;
  }
}

function rememberPublished(profile) {
  publishedLogins.add(profile.login.toLowerCase());
  rememberVerified(profile, profile.issueNumber);
}

function galleryLayout() {
  const pending = [...verifiedProfiles.values()].filter((profile) => profile.pendingPublication &&
    !excludedProfiles.has(profile.login.toLowerCase()));
  const pendingPages = Math.ceil(pending.length / galleryPageSize);
  const publishedPages = profileIndex ? (profileIndex.total ? profileIndex.pageCount : 0) : 1;
  return { pending, pendingPages, pageCount: Math.max(1, pendingPages + publishedPages) };
}

function selectedGalleryPage() {
  return galleryView.page + (galleryView.source === "published" ? galleryLayout().pendingPages : 0);
}

function renderGallery() {
  const { pending, pendingPages, pageCount } = galleryLayout();
  // Publication or exclusion can remove the selected pending page.
  if (galleryView.source === "pending" && galleryView.page > pendingPages) {
    galleryView = pendingPages ? { source: "pending", page: pendingPages } : { source: "published", page: 1 };
  }
  if (galleryView.source === "published" && profileIndex?.total === 0 && pendingPages) {
    galleryView = { source: "pending", page: 1 };
  }
  const isPending = galleryView.source === "pending";
  const profiles = isPending ? [] : (galleryPages.get(galleryView.page) || []).filter((profile) => !excludedProfiles.has(profile.login.toLowerCase()));
  cardResult.replaceChildren(...profiles.map((profile) => renderCard(verifiedProfiles.get(profile.login.toLowerCase()) || profile)));
  const recent = isPending ? pending.slice((galleryView.page - 1) * galleryPageSize, galleryView.page * galleryPageSize) : [];
  document.querySelector("#recent-cards").replaceChildren(...recent.map(renderCard));
  const options = Array.from({ length: pageCount }, (_, index) => {
    const option = document.createElement("option");
    option.value = String(index + 1);
    option.textContent = `${index + 1} / ${pageCount}`;
    return option;
  });
  pageSelect.replaceChildren(...options);
  const cardPage = selectedGalleryPage();
  const pageUnknown = !profileIndex && galleryView.source === "published";
  pageSelect.value = String(cardPage);
  pageSelect.disabled = galleryLoading || pageUnknown || pageCount === 1;
  previousPage.disabled = galleryLoading || pageUnknown || cardPage === 1;
  nextPage.disabled = galleryLoading || pageUnknown || cardPage === pageCount;
  pageSummary.textContent = pageUnknown ? (galleryLoading ? "正在读取页码…" : "页码暂不可用") : `第 ${cardPage} / ${pageCount} 页`;
  galleryRetry.hidden = !galleryError && !publicationError;
  galleryRetry.disabled = galleryLoading;
  galleryStatus.hidden = !galleryLoading && !galleryError && !publicationError && !excludedProfiles.size && profiles.length + recent.length > 0;
  if (galleryLoading) {
    galleryStatus.textContent = "正在读取已核验账号…";
  } else if (galleryError) {
    galleryStatus.textContent = galleryError;
  } else if (publicationError) {
    galleryStatus.textContent = publicationError;
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
    const snapshot = gallerySnapshotId;
    const pending = readJson(new URL("index.json", profileRoot).href, request).then((index) => {
      if (!index || !Number.isSafeInteger(index.total) || index.total < 0 ||
          index.pageSize !== galleryPageSize ||
          (index.snapshotId !== undefined && (typeof index.snapshotId !== "string" || !uuidPattern.test(index.snapshotId))) ||
          index.pageCount !== Math.max(1, Math.ceil(index.total / index.pageSize))) {
        throw new Error("Invalid profile index");
      }
      if (snapshot === gallerySnapshotId) profileIndex = index;
      return index;
    }).finally(() => { if (indexPromise === pending) indexPromise = undefined; });
    indexPromise = pending;
  }
  return indexPromise;
}

function validateIndexedProfile(profile) {
  if (!profile || profile.status !== "verified" || profile.profilePageStatus !== 404 || normalizedLogin(profile.login || "") !== profile.login ||
      !Number.isSafeInteger(profile.issueNumber) || profile.issueNumber < 1 ||
      (profile.snapshotId !== undefined && (typeof profile.snapshotId !== "string" || !uuidPattern.test(profile.snapshotId))) ||
      !uuidPattern.test(profile.requestId || "")) throw new Error("Invalid indexed profile");
  // Snapshot files are published by the repository's trusted indexing workflow.
  validateProfileFields(profile);
  return profile;
}

async function readIndexedProfile(request) {
  const indexed = await readJson(new URL(`users/${request.login.toLowerCase()}.json`, profileRoot).href, request, true);
  if (!indexed) return null;
  const profile = validateIndexedProfile(indexed);
  if (profile.login.toLowerCase() !== request.login.toLowerCase()) throw new Error("Indexed profile does not match the requested login");
  return profile;
}

async function loadGallery(view = galleryView) {
  const loadId = ++galleryLoadId;
  const snapshot = gallerySnapshotId;
  galleryLoading = true;
  galleryError = "";
  renderGallery();
  const request = { controller: new AbortController() };
  try {
    const index = await loadProfileIndex();
    if (snapshot !== gallerySnapshotId) return;
    // A new publication can remove pages after excluded accounts are deleted.
    if (view.source === "published" && view.page > index.pageCount) view = { source: "published", page: 1 };
    if (view.source === "published" && !galleryPages.has(view.page)) {
      const profiles = await readJson(new URL(`pages/${view.page}.json`, profileRoot).href, request);
      if (snapshot !== gallerySnapshotId) return;
      const expectedCount = Math.min(index.pageSize, index.total - (view.page - 1) * index.pageSize);
      if (!Array.isArray(profiles) || profiles.length !== expectedCount) throw new Error("Invalid profile page");
      const validated = profiles.map(validateIndexedProfile);
      if (validated.some((profile) => profile.snapshotId !== index.snapshotId)) throw new Error("Profile page belongs to a different snapshot");
      for (const profile of validated) rememberPublished(profile);
      galleryPages.set(view.page, validated);
    }
    if (loadId === galleryLoadId) galleryView = view;
  } catch (error) {
    if (loadId === galleryLoadId) {
      galleryError = error instanceof ApiError && error.status === 404
        ? "账号列表尚未发布，请稍后重新读取。"
        : "账号列表暂时无法读取，请重新读取。";
    }
  } finally {
    if (loadId === galleryLoadId) {
      galleryLoading = false;
    }
    renderGallery();
  }
}

async function changeCardPage(page) {
  const { pendingPages } = galleryLayout();
  const view = page <= pendingPages ? { source: "pending", page } : { source: "published", page: page - pendingPages };
  if (view.source === "pending") {
    galleryLoadId += 1;
    galleryLoading = false;
    galleryView = view;
    renderGallery();
  } else {
    await loadGallery(view);
  }
  document.querySelector("#gallery-heading").scrollIntoView({ block: "start" });
}

async function refreshGallerySnapshot(view = { source: "published", page: 1 }) {
  // A publication can change both the index and its pages. Retry the complete
  // snapshot rather than retaining metadata from the failed read.
  gallerySnapshotId += 1;
  profileIndex = undefined;
  indexPromise = undefined;
  galleryPages.clear();
  publishedLogins.clear();
  const pending = galleryLayout().pending;
  verifiedProfiles.clear();
  for (const profile of pending) rememberVerified(profile, profile.issueNumber);
  galleryView = view;
  await loadGallery(view);
}

async function reloadGallery() {
  publicationError = "";
  await refreshGallerySnapshot();
  const request = activeRequest;
  const profile = currentProfile;
  if (request && profile?.status === "verified" && profile.profilePageStatus === 404) {
    await updatePublication(request, profile);
  }
}

async function updatePublication(request, result, indexedProfile) {
  const key = result.login.toLowerCase();
  try {
    if (indexedProfile || !publishedLogins.has(key)) {
      const indexed = indexedProfile || await readIndexedProfile(request);
      if (!current(request)) return;
      if (indexed) {
        await loadProfileIndex();
        if (!current(request)) return;
        const sameSnapshot = () => indexed.snapshotId && indexed.snapshotId === profileIndex?.snapshotId;
        if (!sameSnapshot()) {
          const view = galleryView.source === "published" ? galleryView : { source: "published", page: 1 };
          await refreshGallerySnapshot(view);
          if (!current(request)) return;
          // Legacy files have no snapshot ID. A freshly loaded page containing
          // this account is still direct evidence; otherwise ask for a reload.
          if (!sameSnapshot() && !(!indexed.snapshotId && !profileIndex?.snapshotId && publishedLogins.has(key))) {
            throw new Error("The user file and gallery index do not describe the same snapshot");
          }
        }
        rememberPublished(indexed);
      }
    }
    publicationError = "";
    rememberVerified({ ...result, pendingPublication: !publishedLogins.has(key) }, result.issueNumber);
    const newest = verifiedProfiles.get(key);
    if (newest && Date.parse(newest.observedAt) > Date.parse(currentProfile.observedAt)) {
      currentProfile = newest;
      currentCard.replaceChildren(renderCard(currentProfile));
    }
    const { pending } = galleryLayout();
    const position = pending.findIndex((profile) => profile.login.toLowerCase() === key);
    if (position >= 0) {
      galleryLoadId += 1;
      galleryLoading = false;
      galleryView = { source: "pending", page: Math.floor(position / galleryPageSize) + 1 };
    }
    renderGallery();
  } catch (error) {
    if (!current(request)) return;
    publicationError = "资料卡已读取，但暂时无法确认账号列表的发布状态，请重新读取账号列表。";
    renderGallery();
  }
}

async function complete(request, result, indexedProfile) {
  clearTimeout(request.timer);
  resumeButton.hidden = true;
  if (result.status === "excluded") {
    excludedProfiles.add(result.login.toLowerCase());
    verifiedProfiles.delete(result.login.toLowerCase());
    renderGallery();
    const profile = await lookupPublicProfile(request);
    if (!current(request)) return;
    if (profile) showPublicProfile(profile);
    else showStatus("已查到主页核验记录", `@${result.login} · 核验时主页返回 HTTP ${result.profilePageStatus}，未收录到英灵殿。`, "success");
    return;
  }
  if (result.status === "verified") {
    const known = verifiedProfiles.get(result.login.toLowerCase());
    currentProfile = known && Date.parse(known.observedAt) > Date.parse(result.observedAt)
      ? known : { ...result, issueNumber: request.issueNumber };
    currentCard.replaceChildren(renderCard(currentProfile));
    // The compact card already contains the same request link.
    issueLink.hidden = true;
    if (result.profilePageStatus === 404) {
      excludedProfiles.delete(result.login.toLowerCase());
    }
    showStatus("已找到资料卡", result.profilePageStatus === 404
      ? `@${result.login} · 资料卡已显示在搜索框下，可以抬走了`
      : `@${result.login} · 已显示历史资料；记录未确认主页返回 404，因此不加入英灵殿。`, "success");
    if (result.profilePageStatus === 404) await updatePublication(request, currentProfile, indexedProfile);
  } else if (result.status === "unavailable") {
    showStatus("本次未能在来源页确认资料卡", typeof result.message === "string" && result.message ? result.message : "来源页未返回此用户的可用资料卡；这不代表账号不存在。", "unavailable");
  } else {
    showStatus("本次核验未完成", typeof result.message === "string" && result.message ? result.message : "核验服务执行失败，请打开“查看查询请求”了解详情。", "error");
  }
}

function pauseForError(request, error) {
  resumeButton.hidden = false;
  if (error instanceof ApiError && (error.status === 403 || error.status === 429)) {
    request.retryAt = error.retryAt;
    const when = error.retryAt ? `请在 ${new Date(error.retryAt).toLocaleTimeString("zh-CN", { hour12: false })} 后点击“${resumeButton.textContent}”。` : `请稍后点击“${resumeButton.textContent}”。`;
    showStatus("GitHub 暂时限制了结果读取", `${when} 这不表示用户资料不可用。`, "error");
  } else if (error instanceof ApiError && error.status === 404) {
    showStatus("查询请求暂时无法读取", `请确认请求链接有效且公开，再点击“${resumeButton.textContent}”。`, "error");
  } else {
    showStatus("暂时无法读取查询结果", `网络或服务响应异常，恢复后点击“${resumeButton.textContent}”。`, "error");
  }
}

async function poll(request) {
  if (!current(request) || request.polling) return;
  if (Date.now() >= request.deadline) {
    showStatus("本次等待已暂停", request.issueNumber ? "任务可能仍在排队，可点击“继续等待核验结果”。" : "尚未找到提交记录，确认已在 GitHub 提交后，可点击“检查提交结果”。", "unavailable");
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
      if (result) { await complete(request, result); return; }
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
  resumeButton.hidden = false;
  resumeButton.textContent = "检查提交结果";
  history.replaceState(null, "", `?${new URLSearchParams({ login: request.login })}`);
  submitPrivacy.hidden = false;
  showStatus("暂未找到可展示的资料", `@${request.login} · 公开接口未返回资料，可前往 GitHub 提交 Release 资料卡核验，**也可能是你写错了名字**`, "unavailable");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const login = normalizedLogin(loginInput.value);
  loginInput.setCustomValidity(login ? "" : "请输入有效的 GitHub 用户名：最多 39 个字母、数字或单个连字符。");
  if (!login) { loginInput.reportValidity(); return; }
  loginInput.value = login;
  resetRequest();
  history.replaceState(null, "", `?${new URLSearchParams({ login })}`);
  const request = makeRequest(login, crypto.randomUUID());
  activeRequest = request;
  showStatus("正在查询已有资料", `@${login} · 优先读取已核验账号，无需重复提交。`);
  try {
    let profile = verifiedProfiles.get(login.toLowerCase());
    let indexedProfile;
    const cachedPublic = publicProfiles.get(login.toLowerCase());
    if (!profile && cachedPublic) { showPublicProfile(cachedPublic); return; }
    if (!profile && !excludedProfiles.has(login.toLowerCase())) {
      profile = await readIndexedProfile(request);
      if (!current(request)) return;
      indexedProfile = profile;
    }
    if (!current(request)) return;
    if (profile) {
      request.requestId = profile.requestId;
      request.login = profile.login;
      issueFound(request, profile.issueNumber);
      await complete(request, profile, indexedProfile);
      return;
    }
    const publicProfile = publicProfiles.get(login.toLowerCase()) || await lookupPublicProfile(request);
    if (!current(request)) return;
    if (publicProfile) { showPublicProfile(publicProfile); return; }
    submitVerification(request);
  } catch (error) {
    if (!current(request)) return;
    showStatus("暂时无法读取资料", error instanceof ApiError && [403, 429].includes(error.status)
      ? "可能是你输错了名字，或者GitHub 暂时限制了查询频率，请稍后查询。" : "网络或服务响应异常，请稍后查询。", "error");
  }
});

loginInput.addEventListener("input", () => loginInput.setCustomValidity(""));
previousPage.addEventListener("click", () => changeCardPage(selectedGalleryPage() - 1));
nextPage.addEventListener("click", () => changeCardPage(selectedGalleryPage() + 1));
pageSelect.addEventListener("change", () => changeCardPage(Number(pageSelect.value)));
galleryRetry.addEventListener("click", reloadGallery);
submitLink.addEventListener("click", () => {
  const request = activeRequest;
  clearTimeout(request.timer);
  request.startedAt = Date.now();
  request.deadline = request.startedAt + 10 * 60 * 1000;
  resumeButton.hidden = false;
  resumeButton.textContent = "检查提交结果";
  showStatus("请在 GitHub 确认提交", "提交后返回此页，系统会检查核验结果。", "waiting");
  poll(request);
});

resumeButton.addEventListener("click", () => {
  const request = activeRequest;
  if (!request || request.polling) return;
  if (request.retryAt > Date.now()) {
    showStatus("请等待 GitHub 恢复读取额度", `可在 ${new Date(request.retryAt).toLocaleTimeString("zh-CN", { hour12: false })} 后继续等待。`, "error");
    return;
  }
  request.deadline = Date.now() + 10 * 60 * 1000;
  clearTimeout(request.timer);
  showStatus("正在继续等待", request.issueNumber ? "正在读取核验结果。" : "正在查找已提交的 GitHub 查询请求。");
  poll(request);
});

async function initialize() {
  const parameters = new URLSearchParams(location.search);
  const login = normalizedLogin(parameters.get("login") || "");
  if (login) loginInput.value = login;
  const issue = parameters.get("request");
  if (issue !== null) {
    if (!/^[1-9]\d*$/.test(issue) || !Number.isSafeInteger(Number(issue))) {
      showStatus("查询链接无效", "请使用完整的查询请求链接，或输入用户名查询。", "error");
      return;
    }
    const request = makeRequest("", "", Number(issue));
    activeRequest = request;
    showStatus("正在读取查询请求", "正在从 GitHub 获取本次请求与核验结果。");
    poll(request);
    return;
  }
  if (login) {
    form.requestSubmit();
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
