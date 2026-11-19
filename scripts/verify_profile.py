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
    request_id = str(UUID(identifiers[0]))
    return ProfileRequest(number, request_id, login)


class GitHubClient:
    def __init__(self, token):
        if not token:
            raise VerificationError("GITHUB_TOKEN is required.")
        self.token = token

    def request(self, method, path, payload=None):
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


def update_release(client, login):
    release = client.request("GET", f"/releases/tags/{TAG}")
    release_id = release.get("id")
    if (
        type(release_id) is not int or release_id < 1
        or release.get("tag_name") != TAG or release.get("draft") is not False
        or release.get("html_url") != RELEASE_URL
    ):
        raise VerificationError("The fixed public release could not be confirmed.")
    body = release.get("body")
    updated = add_mention(body, login)
    if updated != body:
        client.request("PATCH", f"/releases/{release_id}", {"body": updated})


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


def observe_profile(login, client=None):
    # The browser has a fresh anonymous context. The REST token is never provided to it.
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="en-US", viewport={"width": 1280, "height": 900})
            page.set_default_timeout(15000)
            updated = False
            for attempt in range(7 if client else 1):
                response = page.goto(RELEASE_URL, wait_until="domcontentloaded", timeout=30000)
                if response is None or response.status >= 400 or page.url != RELEASE_URL:
                    raise VerificationError("GitHub's public release page could not be loaded.")
                heading = page.get_by_role("heading", name="Contributors", exact=True)
                contributors = page.locator(".Box-footer").filter(has=heading)
                links = contributors.locator('a[data-hovercard-type="user"]')
                for index in range(links.count()):
                    link = links.nth(index)
                    if not is_profile_url(link.get_attribute("href"), login):
                        continue
                    link.hover()
                    card = page.get_by_role("region", name="User Hovercard", exact=True)
                    try:
                        card.wait_for(state="visible", timeout=15000)
                    except PlaywrightTimeoutError:
                        return {"status": "unavailable", "message": "GitHub did not show a public hovercard for this contributor."}
                    return {"status": "verified", "message": "Read from the contributor's native GitHub hovercard.", **read_hovercard(card, login)}
                if client and not updated:
                    # Read existing contributors first; only a missing login needs a notes edit.
                    update_release(client, login)
                    updated = True
                elif attempt < 6 and client:
                    # GitHub updates the native Contributors list asynchronously after a notes edit.
                    page.wait_for_timeout(5000)
            return {"status": "unavailable", "message": "GitHub did not expose this login in the public Contributors list after the release update."}
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
        "releaseUrl": RELEASE_URL,
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
            and result.get("login") == request.login and result.get("releaseUrl") == RELEASE_URL
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
