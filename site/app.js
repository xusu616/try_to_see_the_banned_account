"use strict";

const repository = "https://github.com/xusu616/try_to_see_the_banned_account";
const cardTemplate = document.querySelector("#card-template");
const grid = document.querySelector("#card-grid");
const archiveStatus = document.querySelector("#archive-status");
const form = document.querySelector("#card-form");
const formStatus = document.querySelector("#form-status");
const proposal = document.querySelector("#proposal");
const contribute = document.querySelector("#contribute");
const releasePath = /^\/[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\/[A-Za-z0-9._-]+\/releases\/tag\/(.+)$/;
let submittedFile;

function httpsUrl(value, hostname) {
  const url = new URL(value);
  if (url.origin !== `https://${hostname}` || url.username || url.password) {
    throw new Error("Unexpected URL origin");
  }
  return url;
}

function sourceTag(value) {
  const url = httpsUrl(value, "github.com");
  const match = url.pathname.match(releasePath);
  if (!match || url.search || url.hash) throw new Error("Invalid Release or Tag URL");
  return decodeURIComponent(match[1]);
}

function avatarAddress(value) {
  const url = httpsUrl(value, "avatars.githubusercontent.com");
  if (!/^\/u\/\d+(?:\?(?:s|v)=\d+(?:&(?:s|v)=\d+)?)?$/.test(url.pathname + url.search) || url.hash) {
    throw new Error("Invalid avatar URL");
  }
  return url.href;
}

function cardElement(card) {
  const element = cardTemplate.content.firstElementChild.cloneNode(true);
  element.querySelector(".profile-name").textContent = card.name;
  element.querySelector(".profile-login").textContent = `@${card.login}`;
  element.querySelector(".avatar-initial").textContent = card.login[0].toUpperCase();

  const avatar = element.querySelector("img");
  avatar.addEventListener("error", () => { avatar.hidden = true; }, { once: true });
  avatar.src = avatarAddress(card.avatarUrl);

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

  const tag = sourceTag(card.releaseUrl);
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
  const requestedCard = new URLSearchParams(location.search).get("card");
  if (requestedCard) document.querySelector("#all-cards").hidden = false;

  try {
    const response = await fetch("./cards.json");
    if (!response.ok) throw new Error("无法读取已发布的资料卡");
    const cards = await response.json();
    const visibleCards = requestedCard ? cards.filter((card) => card.id === requestedCard) : cards;
    if (!visibleCards.length) {
      archiveStatus.textContent = requestedCard ? "没有找到这张资料卡。" : "还没有收录资料卡。";
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
  proposal.hidden = true;
  formStatus.hidden = true;
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  proposal.hidden = true;

  const input = new FormData(form);
  const login = input.get("login").trim();
  let releaseUrl;
  let tag;
  try {
    releaseUrl = httpsUrl(input.get("releaseUrl").trim(), "github.com").href;
    tag = sourceTag(releaseUrl);
  } catch {
    formStatus.textContent = "请填写 GitHub 的 Release 或 Tag 链接，去掉链接末尾的查询参数和锚点。";
    formStatus.hidden = false;
    return;
  }

  let avatarUrl;
  try {
    avatarUrl = avatarAddress(input.get("avatarUrl").trim());
  } catch {
    formStatus.textContent = "请填写 avatars.githubusercontent.com/u/ 下的公开 GitHub 头像链接。";
    formStatus.hidden = false;
    return;
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
  const filename = filenameFor(login, tag, releaseUrl);
  const value = `${JSON.stringify(record, null, 2)}\n`;
  submittedFile = { filename, value };

  const editor = new URL(`${repository}/new/main/cards`);
  editor.search = new URLSearchParams({ filename, value });
  document.querySelector("#github-editor").href = editor.href;
  document.querySelector("#proposal-filename").textContent = `cards/${filename}`;
  document.querySelector("#preview").replaceChildren(cardElement(record));
  formStatus.hidden = true;
  proposal.hidden = false;
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
