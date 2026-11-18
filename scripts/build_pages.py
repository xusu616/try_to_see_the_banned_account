import argparse
from datetime import date
import json
from pathlib import Path
import re
import shutil
from urllib.parse import unquote


FIELDS = {"login", "name", "avatarUrl", "bio", "badge", "releaseUrl", "observedAt"}
SITE_FILES = ("index.html", "styles.css", "app.js")
LOGIN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE | re.ASCII)
CARD_ID = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,98}[a-z0-9])?")
AVATAR_URL = re.compile(
    r"https://avatars\.githubusercontent\.com/u/[0-9]+"
    r"(?:\?(?:s|v)=[0-9]+(?:&(?:s|v)=[0-9]+)?)?"
)
RELEASE_URL = re.compile(
    r"https://github\.com/([^/]+)/([^/]+)/releases/tag/"
    r"((?:[A-Za-z0-9._~!$&'()*+,;=:@/-]|%[0-9A-Fa-f]{2})+)"
)


def valid_login(login):
    return len(login) <= 39 and LOGIN.fullmatch(login) is not None


def reject_repeated_fields(pairs):
    fields = {}
    for field, value in pairs:
        if field in fields:
            raise ValueError(f"字段 {field} 重复")
        fields[field] = value
    return fields


def validate_text(value, field, limit, multiline=False):
    if len(value) > limit:
        raise ValueError(f"{field} 最多允许 {limit} 个字符")
    for character in value:
        number = ord(character)
        if multiline and character in "\n\t":
            continue
        if number < 32 or number == 127 or 0xD800 <= number <= 0xDFFF:
            raise ValueError(f"{field} 含不支持的字符")


def release_key(url):
    match = RELEASE_URL.fullmatch(url)
    if not match:
        raise ValueError("releaseUrl 须为公开 GitHub Tag 或 Release 的完整链接")
    owner, repo, encoded_tag = match.groups()
    if not valid_login(owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo) or repo in {".", ".."}:
        raise ValueError("releaseUrl 的仓库路径无效")
    tag = unquote(encoded_tag, errors="strict")
    validate_text(tag, "Tag", 500)
    if (
        tag.endswith(".") or tag == "@" or ".." in tag or "@{" in tag
        or any(character in tag for character in " ~^:?*[\\")
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in tag.split("/"))
    ):
        raise ValueError("releaseUrl 的 Tag 名称无效")
    return owner.casefold(), repo.casefold(), tag


def load_card(path):
    if not CARD_ID.fullmatch(path.stem):
        raise ValueError("文件名只支持小写字母、数字、点和横线，且以字母或数字起止")
    if path.is_symlink():
        raise ValueError("资料卡须为 JSON 文件")
    card = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_repeated_fields)
    if not isinstance(card, dict) or set(card) != FIELDS:
        raise ValueError(f"须包含且只包含这些字段：{', '.join(sorted(FIELDS))}")
    if any(not isinstance(value, str) for value in card.values()):
        raise ValueError("所有资料卡字段须为字符串")

    if not valid_login(card["login"]):
        raise ValueError("login 须为有效的 GitHub 用户名")
    validate_text(card["name"], "name", 100)
    if not card["name"].strip():
        raise ValueError("name 须为非空显示名称")
    validate_text(card["bio"], "bio", 500, multiline=True)
    if card["badge"] not in {"", "PRO"}:
        raise ValueError("badge 须为空字符串或 PRO")
    if not AVATAR_URL.fullmatch(card["avatarUrl"]):
        raise ValueError("avatarUrl 须为 avatars.githubusercontent.com 上的公开用户头像链接")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", card["observedAt"]):
        raise ValueError("observedAt 须用 YYYY-MM-DD 格式")
    try:
        date.fromisoformat(card["observedAt"])
    except ValueError as error:
        raise ValueError("observedAt 的日期无效") from error

    key = release_key(card["releaseUrl"])
    return {"id": path.stem, **card}, key


def build(root, output):
    if output == root or output.is_relative_to(root / "cards") or output.is_relative_to(root / "site"):
        raise ValueError("请为构建结果指定独立目录，例如 dist/")
    cards = []
    releases = {}
    card_directory = root / "cards"
    if not card_directory.is_dir():
        raise ValueError("找不到 cards/ 目录")
    for path in sorted(card_directory.glob("*.json")):
        try:
            card, key = load_card(path)
            if key in releases:
                raise ValueError(f"releaseUrl 已在 {releases[key]} 使用")
        except (OSError, ValueError) as error:
            raise ValueError(f"cards/{path.name}：{error}") from error
        releases[key] = path.name
        cards.append(card)

    cards.sort(key=lambda card: card["observedAt"], reverse=True)
    static_files = [root / "site" / name for name in SITE_FILES]
    for source in static_files:
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"缺少站点文件 site/{source.name}")

    output.mkdir(parents=True, exist_ok=True)
    for source in static_files:
        shutil.copyfile(source, output / source.name)
    (output / "cards.json").write_text(json.dumps(cards, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(cards)


def main():
    parser = argparse.ArgumentParser(description="校验资料卡并构建静态 Pages 站点。")
    parser.add_argument("--output", required=True, type=Path, help="站点输出目录")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    try:
        count = build(root, args.output.resolve())
    except (OSError, ValueError) as error:
        parser.exit(1, f"Pages 构建失败：{error}\n")
    print(f"已构建 {count} 张资料卡：{args.output.resolve()}")


if __name__ == "__main__":
    main()
