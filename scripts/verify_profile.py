"""Verify a requested profile using only GitHub's normal public release UI."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from uuid import UUID


REPOSITORY = "xusu616/try_to_see_the_banned_account"
TAG = "v0.1.1"
RELEASE_URL = f"https://github.com/{REPOSITORY}/releases/tag/{TAG}"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}"
REQUEST_MARKER = "<!-- profile-card-request -->"
RESULT_MARKER = "<!-- profile-card-result -->"
MENTIONS_START = "<!-- profile-card-mentions:start -->"
MENTIONS_END = "<!-- profile-card-mentions:end -->"
BATCH_MARKER = "<!-- profile-card-batch -->"
RELEASE_CAPACITY = 6
USERNAME = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", re.ASCII)
REQUEST_ID = re.compile(
    r"^Request ID: ([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\r?$", re.MULTILINE
)
RESULT_JSON = re.compile(r"\A<!-- profile-card-result -->\n```json\n(.*?)\n```\n?\Z", re.DOTALL)


class VerificationError(RuntimeError):
    """A failure with a safe, public explanation for the request issue."""


@dataclass(frozen=True)
class ProfileRequest:
    issue_number: int
    request_id: str
    login: str


def valid_login(login):
    return isinstance(login, str) and 1 <= len(login) <= 39 and USERNAME.fullmatch(login) is not None


def valid_request_id(value):
    return isinstance(value, str) and re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        value, re.IGNORECASE | re.ASCII,
    ) is not None


class NoProfileRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def profile_page_status(login):
    if not valid_login(login):
        raise VerificationError("The GitHub login is invalid.")
    request = Request(
        f"https://github.com/{login}",
        headers={"Accept": "text/html", "User-Agent": "Public-Profile-Card-Verification"},
    )
    try:
        # Check the original profile URL. A redirect to another 404 is not eligible.
        with build_opener(NoProfileRedirect()).open(request, timeout=15) as response:
            return response.status
    except HTTPError as error:
        status = error.code
        error.close()
        if status in (403, 429) or status >= 500:
            raise VerificationError(f"The profile page check returned HTTP {status}; its visibility could not be confirmed.") from None
        return status
    except (URLError, TimeoutError) as error:
        raise VerificationError("The profile page could not be checked; its visibility is unknown.") from error


def parse_request(issue):
    if not isinstance(issue, dict) or "pull_request" in issue:
        raise VerificationError("Only a profile request issue is accepted.")
    number = issue.get("number")
    if type(number) is not int or number < 1:
        raise VerificationError("The request issue number is invalid.")
    title = issue.get("title")
    if not isinstance(title, str) or not title.startswith("Verify profile: "):
        raise VerificationError("The request title must be Verify profile: <login>.")
    login = title.removeprefix("Verify profile: ")
    if not valid_login(login):
        raise VerificationError("The GitHub login is invalid.")
    body = issue.get("body")
    if not isinstance(body, str) or body.count(REQUEST_MARKER) != 1:
        raise VerificationError("The profile request marker is missing or duplicated.")
    identifiers = REQUEST_ID.findall(body)
    if len(identifiers) != 1 or len(re.findall(r"^Request ID:", body, re.MULTILINE)) != 1:
        raise VerificationError("The request must contain exactly one canonical UUID.")
    if not valid_request_id(identifiers[0]):
        raise VerificationError("The request ID must be a UUID v4 with the standard variant.")
    request_id = str(UUID(identifiers[0]))
    return ProfileRequest(number, request_id, login)


class GitHubClient:
    def __init__(self, token):
        if not token:
            raise VerificationError("GITHUB_TOKEN is required.")
        self.token = token

    def request(self, method, path, payload=None, allow_missing=False):
        # Paths are built exclusively from fixed strings and validated numeric IDs.
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            API_ROOT + path,
            method=method,
            data=data,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "Public-Profile-Card-Verification",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            if allow_missing and error.code == 404:
                error.close()
                return None
            # Never publish response bodies, headers, credentials, or request objects.
            raise VerificationError(f"GitHub REST API returned HTTP {error.code}.") from None
        except (URLError, TimeoutError) as error:
            raise VerificationError("GitHub REST API could not be reached.") from error

    def comments(self, issue_number):
        page = 1
        while True:
            items = self.request("GET", f"/issues/{issue_number}/comments?per_page=100&page={page}")
            yield from items
            if len(items) < 100:
                return
            page += 1


def load_request(event, event_name, repository, client):
    if repository != REPOSITORY or event.get("repository", {}).get("full_name") != REPOSITORY:
        raise VerificationError("This worker only operates in its fixed repository.")
    if event_name == "issues" and event.get("action") == "opened":
        return parse_request(event.get("issue"))
    if event_name == "workflow_dispatch":
        number = event.get("inputs", {}).get("issue_number", "")
        if not isinstance(number, str) or not re.fullmatch(r"[1-9][0-9]*", number, re.ASCII):
            raise VerificationError("workflow_dispatch requires a positive issue_number.")
        return parse_request(client.request("GET", f"/issues/{int(number)}"))
    raise VerificationError("Unsupported workflow event.")


def add_mention(body, login):
    if not valid_login(login):
        raise VerificationError("The GitHub login is invalid.")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise VerificationError("The release notes are not text.")
    starts, ends = body.count(MENTIONS_START), body.count(MENTIONS_END)
    if starts == ends == 0:
        separator = "\n\n" if body else ""
        return body + separator + f"{MENTIONS_START}\n### Profile card requests\n\n@{login}\n{MENTIONS_END}"
    if starts != 1 or ends != 1 or body.index(MENTIONS_START) >= body.index(MENTIONS_END):
        raise VerificationError("The managed release section is malformed; the notes were not changed.")
    start = body.index(MENTIONS_START) + len(MENTIONS_START)
    end = body.index(MENTIONS_END)
    section = body[start:end]
    mentions = re.findall(r"^@([A-Za-z0-9-]+)\r?$", section, re.MULTILINE)
    if any(mention.casefold() == login.casefold() for mention in mentions):
        return body
    separator = "" if section.endswith("\n") else "\n"
    return body[:end] + separator + f"@{login}\n" + body[end:]


def is_release_url(value):
    return isinstance(value, str) and re.fullmatch(
        re.escape(f"https://github.com/{REPOSITORY}/releases/tag/") + r"v0\.1\.[1-9][0-9]*", value
    ) is not None


def validate_release(release, tag):
    release_id = release.get("id")
    if (
        type(release_id) is not int or release_id < 1
        or release.get("tag_name") != tag or release.get("draft") is not False
        or release.get("html_url") != f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
        or not is_release_url(release.get("html_url"))
    ):
        raise VerificationError("The public profile-card release could not be confirmed.")
    return release


def release_members(release):
    body = release.get("body") or ""
    # Include mentions outside the managed section in the original Release too.
    # Managed entries reserve slots while GitHub updates Contributors asynchronously.
    return {name.casefold() for name in re.findall(r"(?<![\w/@])@([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)", body) if valid_login(name)}


def release_catalog(client):
    releases, occupied = [], set()
    page = 1
    while True:
        batch = client.request("GET", f"/releases?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise VerificationError("GitHub returned an invalid release collection.")
        for release in batch:
            tag = release.get("tag_name", "")
            if not re.fullmatch(r"v0\.1\.[1-9][0-9]*", tag):
                continue
            occupied.add(int(tag.rsplit(".", 1)[1]))
            if tag == TAG or BATCH_MARKER in (release.get("body") or ""):
                releases.append(validate_release(release, tag))
        if len(batch) < 100:
            break
        page += 1
    if not any(release["tag_name"] == TAG for release in releases):
        raise VerificationError("The original profile-card release is missing.")
    releases.sort(key=lambda release: int(release["tag_name"].rsplit(".", 1)[1]), reverse=True)
    return releases, occupied


def create_profile_release(client, occupied):
    patch_number = max(occupied) + 1
    # A version may already name a tag without a Release; leave that tag alone.
    while client.request("GET", f"/git/ref/tags/v0.1.{patch_number}", allow_missing=True) is not None:
        patch_number += 1
    tag = f"v0.1.{patch_number}"
    release = client.request("POST", "/releases", {
        "tag_name": tag, "target_commitish": "main", "name": f"英灵殿资料卡 · {tag}",
        # Reserve the login separately, so an ambiguous creation response can only
        # leave an empty, reusable batch rather than an untracked account slot.
        "body": f"{BATCH_MARKER}\n\n公开资料卡来源，每批最多 6 个账号。",
        "draft": False, "prerelease": False, "make_latest": "false",
    })
    return validate_release(release, tag)


def update_release(client, login, tag=TAG, contributors=()):
    release = validate_release(client.request("GET", f"/releases/tags/{tag}"), tag)
    members = release_members(release) | set(contributors)
    if login.casefold() not in members and len(members) >= RELEASE_CAPACITY:
        raise VerificationError("This profile-card release already has six accounts.")
    body = release.get("body")
    updated = add_mention(body, login)
    if updated == body:
        return False
    try:
        client.request("PATCH", f"/releases/{release['id']}", {"body": updated})
    except Exception as error:
        # A network failure can happen after GitHub has applied the PATCH.
        cleanup_release_mention(client, login, tag, error)
        raise
    return True


def cleanup_release_mention(client, login, tag, original_error=None):
    """Remove only the managed line this observation added, using fresh notes."""
    try:
        release = validate_release(client.request("GET", f"/releases/tags/{tag}"), tag)
        body = release.get("body") or ""
        starts, ends = body.count(MENTIONS_START), body.count(MENTIONS_END)
        if starts == ends == 0:
            return
        if starts != 1 or ends != 1 or body.index(MENTIONS_START) >= body.index(MENTIONS_END):
            raise VerificationError("The managed release section is malformed; the reservation could not be removed.")
        start = body.index(MENTIONS_START) + len(MENTIONS_START)
        end = body.index(MENTIONS_END)
        section = re.sub(rf"^@{re.escape(login)}\r?\n", "", body[start:end], count=1, flags=re.IGNORECASE | re.MULTILINE)
        updated = body[:start] + section + body[end:]
        if updated != body:
            client.request("PATCH", f"/releases/{release['id']}", {"body": updated})
    except Exception as cleanup_error:
        if original_error is not None:
            raise ExceptionGroup("Profile verification and Release reservation cleanup both failed.", [original_error, cleanup_error]) from None
        raise


def is_profile_url(value, login):
    if not isinstance(value, str):
        return False
    url = urlsplit(urljoin("https://github.com", value))
    return (
        url.scheme == "https" and url.netloc == "github.com"
        and url.path.rstrip("/").casefold() == f"/{login.casefold()}"
        and not url.query and not url.fragment
    )


def is_avatar_url(value):
    if not isinstance(value, str):
        return False
    url = urlsplit(value)
    return (
        url.scheme == "https" and url.netloc == "avatars.githubusercontent.com"
        and re.fullmatch(r"/(?:u|in)/[0-9]+", url.path, re.ASCII) is not None
        and not url.fragment
    )


def read_hovercard(card, login):
    identity = card.get_by_role("region", name="User login and name", exact=True)
    login_link = identity.locator("a.Link--primary").first
    if (
        not is_profile_url(login_link.get_attribute("href"), login)
        or login_link.inner_text().strip().removeprefix("@").casefold() != login.casefold()
    ):
        raise VerificationError("The visible hovercard belongs to a different GitHub login.")
    name_link = identity.locator("a.Link--secondary")
    name = name_link.first.inner_text().strip() if name_link.count() else ""
    bio_region = card.get_by_role("region", name="User bio", exact=True)
    # inner_text follows GitHub's CSS and can collapse newlines in the public bio.
    bio = bio_region.text_content().strip() if bio_region.count() else ""
    avatar = card.get_by_role("region", name="User avatar", exact=True).locator("img").first
    avatar_url = avatar.get_attribute("src")
    if not is_avatar_url(avatar_url):
        raise VerificationError("The visible hovercard avatar URL was not a GitHub avatar.")
    badge_region = card.get_by_role("region", name="User pro badge", exact=True)
    badge = "PRO" if badge_region.count() and badge_region.inner_text().strip().upper() == "PRO" else ""
    return {"name": name, "bio": bio, "avatarUrl": avatar_url, "badge": badge}


def observe_profile(login, client=None, source_url=RELEASE_URL):
    # The browser has a fresh anonymous context. The REST token is never provided to it.
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="en-US", viewport={"width": 1280, "height": 900})
            page.set_default_timeout(15000)
            if not is_release_url(source_url):
                raise VerificationError("The profile-card source URL is invalid.")

            def contributors_at(url):
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if response is None or response.status >= 400 or page.url != url:
                    raise VerificationError("GitHub's public release page could not be loaded.")
                heading = page.get_by_role("heading", name="Contributors", exact=True)
                contributors = page.locator(".Box-footer").filter(has=heading)
                links = contributors.locator('a[data-hovercard-type="user"]')
                return links

            def read_matching(links, url):
                for index in range(links.count()):
                    link = links.nth(index)
                    if not is_profile_url(link.get_attribute("href"), login):
                        continue
                    link.hover()
                    card = page.get_by_role("region", name="User Hovercard", exact=True)
                    try:
                        card.wait_for(state="visible", timeout=15000)
                    except PlaywrightTimeoutError:
                        return {"status": "unavailable", "releaseUrl": url, "message": "GitHub did not show a public hovercard for this contributor."}
                    return {"status": "verified", "releaseUrl": url, "message": "Read from the contributor's native GitHub hovercard.", **read_hovercard(card, login)}
                return None

            if not client:
                result = read_matching(contributors_at(source_url), source_url)
                return result or {"status": "unavailable", "releaseUrl": source_url, "message": "The existing contributor did not expose a public hovercard."}

            releases, occupied = release_catalog(client)
            existing = [release for release in releases if login.casefold() in release_members(release)]
            # Metadata locates existing accounts without opening every Release in a browser.
            # The original Release can have contributors outside the managed notes.
            # Check it first so those accounts are not assigned a second slot.
            candidates = existing or (
                [release for release in releases if release["tag_name"] == TAG]
                + [release for release in releases if release["tag_name"] != TAG
                   and len(release_members(release)) < RELEASE_CAPACITY]
            )
            selected = None
            reservation_added = False
            for release in candidates:
                source_url = release["html_url"]
                links = contributors_at(source_url)
                result = read_matching(links, source_url)
                if result:
                    return result
                contributors = set()
                for index in range(links.count()):
                    href = links.nth(index).get_attribute("href")
                    name = urlsplit(urljoin("https://github.com", href or "")).path.strip("/")
                    if valid_login(name) and is_profile_url(href, name):
                        contributors.add(name.casefold())
                if existing or len(release_members(release) | contributors) < RELEASE_CAPACITY:
                    reservation_added = update_release(client, login, release["tag_name"], contributors)
                    selected = release
                    break
            if selected is None:
                selected = create_profile_release(client, occupied)
                reservation_added = update_release(client, login, selected["tag_name"])
            source_url = selected["html_url"]
            # A single reservation is followed by bounded waits for GitHub's Contributors update.
            result = None
            try:
                for attempt in range(6):
                    if attempt:
                        page.wait_for_timeout(5000)
                    result = read_matching(contributors_at(source_url), source_url)
                    if result:
                        break
            except Exception as error:
                if reservation_added:
                    cleanup_release_mention(client, login, selected["tag_name"], error)
                raise
            if reservation_added and (result is None or result["status"] != "verified"):
                cleanup_release_mention(client, login, selected["tag_name"])
            return result or {"status": "unavailable", "releaseUrl": source_url, "message": "GitHub did not expose this login in the public Contributors list after the release update."}
        finally:
            browser.close()


def make_result(request, observation):
    return {
        "requestId": request.request_id,
        "login": request.login,
        "status": observation["status"],
        "name": observation.get("name", ""),
        "bio": observation.get("bio", ""),
        "avatarUrl": observation.get("avatarUrl", ""),
        "badge": observation.get("badge", ""),
        "releaseUrl": observation.get("releaseUrl", RELEASE_URL),
        "observedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "message": observation["message"],
        "bioFormatVersion": 2,
        "profilePageStatus": observation.get("profilePageStatus"),
    }


def result_body(result):
    # JSON escapes keep a public bio from closing the Markdown fence or adding HTML.
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    for character in ("`", "<", ">", "&"):
        encoded = encoded.replace(character, f"\\u{ord(character):04x}")
    return f"{RESULT_MARKER}\n```json\n{encoded}\n```\n"


def previous_result(comments, request):
    for comment in reversed(comments):
        author = comment.get("user", {})
        if author.get("login") != "github-actions[bot]" or author.get("type") != "Bot":
            continue
        body = comment.get("body")
        match = RESULT_JSON.fullmatch(body) if isinstance(body, str) else None
        if not match:
            continue
        try:
            result = json.loads(match[1])
        except json.JSONDecodeError:
            continue
        if (
            isinstance(result, dict) and result.get("requestId") == request.request_id
            and result.get("login") == request.login and is_release_url(result.get("releaseUrl"))
            and result.get("status") in ("verified", "unavailable", "error", "excluded")
        ):
            return comment, result
    return None, None


def finish_request(client, request, result, comment=None):
    body = {"body": result_body(result)}
    if comment is None:
        client.request("POST", f"/issues/{request.issue_number}/comments", body)
    else:
        client.request("PATCH", f"/issues/comments/{comment['id']}", body)
    client.request("PATCH", f"/issues/{request.issue_number}", {"state": "closed"})


def run_verification(client, request, refresh=False):
    comment, prior = previous_result(list(client.comments(request.issue_number)), request)
    stage = "checking the original profile page's HTTP status"
    try:
        page_status = profile_page_status(request.login)
        if page_status != 404:
            observation = {
                "status": "excluded", "profilePageStatus": page_status,
                "message": f"GitHub profile page returned HTTP {page_status}; only profiles returning HTTP 404 can enter the gallery.",
            }
        elif prior and not refresh and (
            prior["status"] == "unavailable"
            or prior["status"] == "verified" and prior.get("bioFormatVersion") == 2 and prior.get("profilePageStatus") == 404
        ):
            client.request("PATCH", f"/issues/{request.issue_number}", {"state": "closed"})
            return 0
        else:
            stage = "checking the existing release contributor and reading its public hovercard"
            observation = {**observe_profile(request.login, client), "profilePageStatus": 404}
    except Exception as error:
        # This is the job's reporting boundary: publish an explicit error and fail the run.
        # Browser/network/DOM failures must never become a successful or unavailable result.
        message = str(error) if isinstance(error, VerificationError) else "Verification failed; inspect the Actions run for the failing stage."
        print(f"Verification failed while {stage} ({type(error).__name__}).", file=sys.stderr)
        traceback.print_exception(error, chain=False)
        observation = {"status": "error", "message": message}
    result = make_result(request, observation)
    finish_request(client, request, result, comment)
    print(f"Request #{request.issue_number}: {result['status']}.")
    return 1 if result["status"] == "error" else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true", help="Validate the event without modifying GitHub or loading a browser.")
    parser.add_argument("--refresh", action="store_true", help="Refresh an existing request's successful result.")
    args = parser.parse_args()
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        client = GitHubClient(os.environ.get("GITHUB_TOKEN"))
        request = load_request(event, os.environ.get("GITHUB_EVENT_NAME"), os.environ.get("GITHUB_REPOSITORY"), client)
    except (KeyError, OSError, ValueError, VerificationError) as error:
        print(str(error) if isinstance(error, VerificationError) else "The GitHub event could not be read.", file=sys.stderr)
        if args.validate:
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write("valid=false\n")
            return 0
        return 1
    if args.validate:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("valid=true\n")
        print(f"Validated request #{request.issue_number}.")
        return 0
    try:
        return run_verification(client, request, refresh=args.refresh)
    except VerificationError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
