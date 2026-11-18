"use strict";

const repository = "https://github.com/xusu616/try_to_see_the_banned_account";
const cardTemplate = document.querySelector("#card-template");
const grid = document.querySelector("#card-grid");
const archiveStatus = document.querySelector("#archive-status");
const form = document.querySelector("#card-form");
const formStatus = document.querySelector("#form-status");
const proposal = document.querySelector("#proposal");
const avatarStatus = document.querySelector("#avatar-status");
const contribute = document.querySelector("#contribute");
const releasePath = /^\/([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)\/([A-Za-z0-9._-]+)\/releases\/tag\/(.+)$/;
const githubLogin = /^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$/;
let submittedFile;
let activeLookup;

function httpsUrl(value, hostname) {
  const url = new URL(value);
  if (url.origin !== `https://${hostname}` || url.username || url.password) {
    throw new Error("Unexpected URL origin");
  }
  return url;
}

function releaseSource(value) {
  const url = httpsUrl(value, "github.com");
  const match = url.pathname.match(releasePath);
  if (!match || url.search || url.hash) throw new Error("Invalid Release or Tag URL");
  const [, owner, repo, encodedTag] = match;
  const tag = decodeURIComponent(encodedTag);
  const invalidPart = tag.split("/").some((part) => !part || part.startsWith(".") || part.endsWith(".lock"));
  if (
    tag === "@" || tag.endsWith(".") || tag.includes("..") || tag.includes("@{") ||
    /[\u0000-\u0020\u007f~^:?*\[\\]/.test(tag) || invalidPart
  ) {
    throw new Error("Invalid Git tag name");
  }
  return { owner, repo, tag };
}

function avatarAddress(value) {
  const url = httpsUrl(value, "avatars.githubusercontent.com");
  if (!/^\/u\/\d+(?:\?(?:s|v)=\d+(?:&(?:s|v)=\d+)?)?$/.test(url.pathname + url.search) || url.hash) {
    throw new Error("Invalid avatar URL");
  }
  return url.href;
}

function showAvatar(image, address) {
  image.src = avatarAddress(address);
  image.hidden = false;
}

async function findReleaseAuthor({ owner, repo, tag }, request) {
  const endpoint = `https://api.github.com/repos/${owner}/${repo}/releases/tags/${encodeURIComponent(tag)}`;
  const timeout = setTimeout(() => request.abort(), 5000);
  try {
    const response = await fetch(endpoint, { signal: request.signal });
    if (!response.ok) return null;
    const { author } = await response.json();
    if (typeof author?.login !== "string" || author.login.length > 39 || !githubLogin.test(author.login)) {
      return null;
    }
    return author;
  } catch {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

function cardElement(card) {
  const element = cardTemplate.content.firstElementChild.cloneNode(true);
  element.querySelector(".profile-name").textContent = card.name;
  element.querySelector(".profile-login").textContent = `@${card.login}`;
  element.querySelector(".avatar-initial").textContent = card.login[0].toUpperCase();

  const avatar = element.querySelector("img");
  avatar.addEventListener("error", () => { avatar.hidden = true; }, { once: true });
  if (card.avatarUrl) showAvatar(avatar, card.avatarUrl);

  if (card.badge) {
    const badge = element.querySelector(".profile-badge");
    badge.hidden = false;
    badge.textContent = card.badge;
  }
  if (card.bio) {
    const bio = element.querySelector(".profile-bio");
    bio.hidden = false;
    bio.textContent = card.bio;
  }

  const { tag } = releaseSource(card.releaseUrl);
  element.querySelector(".release-tag").textContent = tag;
  const releaseLink = element.querySelector(".release-link");
  releaseLink.href = card.releaseUrl;
  releaseLink.title = "在 GitHub 打开来源";
  const time = element.querySelector("time");
  time.dateTime = card.observedAt;
  time.textContent = `记录于 ${card.observedAt}`;

  const permalink = element.querySelector(".permalink");
  if (card.id) permalink.href = `?${new URLSearchParams({ card: card.id })}`;
  else permalink.hidden = true;
  return element;
}

async function loadArchive() {
  const parameters = new URLSearchParams(location.search);
  const requestedCard = parameters.get("card");
  const requestedLogin = (parameters.get("login") ?? "").trim().replace(/^@/, "");
  if (requestedCard || requestedLogin) document.querySelector("#all-cards").hidden = false;
  if (githubLogin.test(requestedLogin)) document.querySelector("#archive-login").value = requestedLogin;

  try {
    const response = await fetch("./cards.json");
    if (!response.ok) throw new Error("无法读取已发布的资料卡");
    const cards = await response.json();
    let visibleCards = cards;
    if (requestedCard) {
      visibleCards = cards.filter((card) => card.id === requestedCard);
    } else if (requestedLogin) {
      const login = requestedLogin.toLowerCase();
      visibleCards = cards.filter((card) => card.login.toLowerCase() === login);
    }
    if (!visibleCards.length) {
      if (requestedCard) archiveStatus.textContent = "没有找到这张资料卡。";
      else if (requestedLogin) archiveStatus.textContent = "档案里暂未收录这个用户名。";
      else archiveStatus.textContent = "还没有收录资料卡。";
      return;
    }
    grid.replaceChildren(...visibleCards.map(cardElement));
    archiveStatus.hidden = true;
  } catch {
    archiveStatus.textContent = "资料卡载入失败，请刷新页面。";
  }
}

function localDate() {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function filenameFor(login, tag, releaseUrl) {
  const tagSlug = tag.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  let hash = 0;
  for (const character of releaseUrl) {
    hash = Math.imul(hash, 31) + character.codePointAt(0) | 0;
  }
  return `${login.toLowerCase()}-${tagSlug || "tag"}-${(hash >>> 0).toString(36)}.json`;
}

document.querySelector("#contribute-link").addEventListener("click", () => { contribute.open = true; });
if (location.hash === "#contribute") contribute.open = true;
form.elements.observedAt.value = localDate();

form.addEventListener("input", () => {
  activeLookup?.abort();
  activeLookup = undefined;
  proposal.hidden = true;
  formStatus.hidden = true;
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  proposal.hidden = true;
  formStatus.hidden = true;
  avatarStatus.hidden = true;
  activeLookup?.abort();
  const lookup = new AbortController();
  activeLookup = lookup;

  const input = new FormData(form);
  let login = input.get("login").trim();
  let releaseUrl;
  let source;
  try {
    releaseUrl = httpsUrl(input.get("releaseUrl").trim(), "github.com").href;
    source = releaseSource(releaseUrl);
  } catch {
    formStatus.textContent = "请填写 GitHub 的 Release 或 Tag 链接，去掉链接末尾的查询参数和锚点。";
    formStatus.hidden = false;
    return;
  }

  let avatarUrl = input.get("avatarUrl").trim();
  try {
    if (avatarUrl) avatarUrl = avatarAddress(avatarUrl);
  } catch {
    formStatus.textContent = "请填写 avatars.githubusercontent.com/u/ 下的公开 GitHub 头像链接。";
    formStatus.hidden = false;
    return;
  }

  let author;
  if (!login) {
    formStatus.textContent = "正在读取 Release 的署名…";
    formStatus.hidden = false;
    author = await findReleaseAuthor(source, lookup);
    if (activeLookup !== lookup) return;
    if (!author) {
      formStatus.textContent = "未能自动读取署名。请填写来源页上显示的 GitHub 用户名。";
      return;
    }
    login = author.login;
    form.elements.login.value = login;
  }

  const record = {
    login,
    name: input.get("name").trim() || login,
    avatarUrl,
    bio: input.get("bio").trim().replace(/\r\n?/g, "\n"),
    badge: input.get("badge"),
    releaseUrl,
    observedAt: input.get("observedAt"),
  };
  const filename = filenameFor(login, source.tag, releaseUrl);
  const value = `${JSON.stringify(record, null, 2)}\n`;
  submittedFile = { filename, value };

  const editor = new URL(`${repository}/new/main/cards`);
  editor.search = new URLSearchParams({ filename, value });
  document.querySelector("#github-editor").href = editor.href;
  document.querySelector("#proposal-filename").textContent = `cards/${filename}`;
  const previewCard = cardElement(record);
  document.querySelector("#preview").replaceChildren(previewCard);
  formStatus.hidden = true;
  proposal.hidden = false;

  if (avatarUrl) return;
  avatarStatus.textContent = "正在读取 Release 的头像…";
  avatarStatus.hidden = false;
  author ??= await findReleaseAuthor(source, lookup);
  if (activeLookup !== lookup) return;
  if (author && author.login.toLowerCase() !== login.toLowerCase()) {
    avatarStatus.textContent = `这个 Release 的署名账号是 @${author.login}。请核对填写的用户名。`;
    return;
  }
  if (author?.avatar_url) {
    try {
      showAvatar(previewCard.querySelector("img"), author.avatar_url);
      avatarStatus.hidden = true;
      return;
    } catch {
      // The preview keeps the login initial when the response uses another avatar host.
    }
  }
  avatarStatus.textContent = "当前预览使用用户名首字母，PR 构建时会尝试从来源页读取头像。";
});

document.querySelector("#download-json").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([submittedFile.value], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = submittedFile.filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

loadArchive();
