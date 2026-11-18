import argparse
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


GITHUB = "https://github.com"
REST_API = "https://api.github.com"
REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+")
USERNAME = re.compile(r"[A-Za-z0-9-]{1,39}")
PAGE = re.compile(r"[^/]+/[^/]+/(?:releases/tag/.+|(?:issues|pull)/\d+|commit/[a-fA-F0-9]+)")


class ProfileLinkParser(HTMLParser):
    def __init__(self, username):
        super().__init__()
        self.profile_path = f"/{username.casefold()}"
        self.in_main = False
        self.found = False

    def handle_starttag(self, tag, attributes):
        if tag == "main":
            self.in_main = True
        if self.in_main and tag == "a":
            attributes = dict(attributes)
            if attributes.get("data-hovercard-type") == "user":
                link = attributes.get("href") or ""
                # An exact profile link distinguishes a login from a display name.
                if link.casefold().rstrip("/") in (self.profile_path, GITHUB + self.profile_path):
                    self.found = True

    def handle_endtag(self, tag):
        if tag == "main":
            self.in_main = False


def public_request(url, accept):
    return urlopen(Request(url, headers={"Accept": accept, "User-Agent": "GitHub-Profile-Card-Links"}), timeout=15)


def list_items(endpoint):
    page = 1
    while True:
        with public_request(f"{REST_API}{endpoint}?per_page=100&page={page}", "application/vnd.github+json") as response:
            items = json.load(response)
            has_next = 'rel="next"' in response.headers.get("Link", "")
        yield from items
        if not has_next:
            return
        page += 1


def inputs_to_check(values):
    for value in values:
        if REPOSITORY.fullmatch(value):
            if value.split("/")[1] in (".", ".."):
                raise ValueError(f"无效的仓库名 {value}")
            yield value, None
            continue
        url = urlsplit(value)
        if url.scheme != "https" or url.netloc.lower() != "github.com":
            raise ValueError(f"请输入 owner/repo 或 GitHub 页面链接，收到 {value}")
        path = url.path.strip("/")
        if REPOSITORY.fullmatch(path):
            yield from inputs_to_check([path])
        elif PAGE.fullmatch(path) and REPOSITORY.fullmatch("/".join(path.split("/")[:2])):
            yield None, f"{GITHUB}/{path}"
        else:
            raise ValueError(f"请输入仓库、Tag、Issue、PR 或提交链接，收到 {value}")


def write_report(path, username, matches, errors, checked):
    links = "\n".join(f'<li><a href="{escape(url)}">{escape(url.removeprefix(GITHUB + "/"))}</a></li>' for url in matches)
    failed = "\n".join(f"<li>{escape(error)}</li>" for error in errors)
    path.write_text(f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(username)} 的 GitHub 资料卡入口</title>
<style>body {{ font: 1rem/1.6 system-ui, sans-serif; margin: 2rem auto; padding: 0 1rem; max-width: 55rem; overflow-wrap: anywhere; color-scheme: light dark; }}</style>
<h1>{escape(username)} 的 GitHub 资料卡入口</h1>
<p>检查了 {checked} 个公开页面，找到 {len(matches)} 个入口。打开链接，把鼠标停在页面里的 {escape(username)} 署名上，由 GitHub 展开资料卡。</p>
<ul>{links}</ul>
{f'<details><summary>{len(errors)} 项未能检查</summary><ul>{failed}</ul></details>' if errors else ''}
</html>
""", encoding="utf-8")


def main():
    cli = argparse.ArgumentParser(description="批量查找指定 GitHub 用户的公开资料卡入口。")
    cli.add_argument("username", help="资料卡所属的 GitHub 用户名")
    cli.add_argument("targets", nargs="*", help="仓库名 owner/repo 或公开 Tag、Issue、PR、提交链接")
    cli.add_argument("--starred", metavar="USER", help="额外检查这个用户公开 Star 的仓库")
    cli.add_argument("--max-tags", type=int, help="每个仓库最多检查多少个 Tag。省略时检查全部")
    cli.add_argument("--output", type=Path, default=Path("profile-card-links.html"), help="HTML 报告路径")
    args = cli.parse_intermixed_args()
    if not USERNAME.fullmatch(args.username) or (args.starred and not USERNAME.fullmatch(args.starred)):
        cli.error("请使用有效的 GitHub 用户名。")
    if not args.targets and not args.starred:
        cli.error("请至少提供一个仓库、页面链接或 --starred。")
    if args.max_tags is not None and args.max_tags < 1:
        cli.error("--max-tags 必须是正整数。")

    try:
        targets = list(inputs_to_check(args.targets))
    except ValueError as error:
        cli.error(str(error))
    repositories = {repo.casefold(): repo for repo, _ in targets if repo}
    pages = {url: None for _, url in targets if url}
    errors = []
    if args.starred:
        try:
            for repo in list_items(f"/users/{args.starred}/starred"):
                name = repo["full_name"]
                repositories.setdefault(name.casefold(), name)
        except (URLError, TimeoutError) as error:
            errors.append(f"{args.starred} 的 Star 列表，{error}")

    for repo in repositories.values():
        print(f"读取 Tags {repo}", file=sys.stderr, flush=True)
        try:
            for index, tag in enumerate(list_items(f"/repos/{repo}/tags"), start=1):
                pages[f"{GITHUB}/{repo}/releases/tag/{quote(tag['name'], safe='')}"] = None
                if index == args.max_tags:
                    break
        except (URLError, TimeoutError) as error:
            errors.append(f"{repo} 的 Tags，{error}")
            if isinstance(error, HTTPError) and error.code in (403, 429):
                break

    matches = []
    checked = 0
    for index, url in enumerate(pages, start=1):
        print(f"[{index}/{len(pages)}] {url}", file=sys.stderr, flush=True)
        try:
            with public_request(url, "text/html") as response:
                if urlsplit(response.url).netloc != "github.com" or urlsplit(response.url).path == "/login":
                    errors.append(f"{url} 跳转出公开页面")
                    continue
                parser = ProfileLinkParser(args.username)
                parser.feed(response.read().decode("utf-8", errors="replace"))
                parser.close()
        except (URLError, TimeoutError) as error:
            errors.append(f"{url}，{error}")
            if isinstance(error, HTTPError) and error.code in (403, 429):
                errors.append(f"GitHub 拒绝继续访问，剩余 {len(pages) - index} 页未检查。")
                break
            continue
        checked += 1
        if parser.found:
            matches.append(url)

    write_report(args.output, args.username, matches, errors, checked)
    print(f"找到 {len(matches)} 个入口，{len(errors)} 项未能检查。报告 {args.output.resolve()}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
