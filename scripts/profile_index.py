"""Incrementally index verified public results and export a paginated Pages snapshot."""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from scripts.verify_profile import (
    API_ROOT, RESULT_JSON, GitHubClient, VerificationError,
    is_avatar_url, is_release_url, parse_request, profile_page_status, valid_request_id,
)


PAGE_SIZE = 6


def open_index(path):
    connection = sqlite3.connect(path)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            login TEXT PRIMARY KEY COLLATE NOCASE,
            observed_at TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            bio_format_version INTEGER NOT NULL,
            result TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS profile_date ON profiles(observed_at DESC, login)")
    connection.execute("CREATE INDEX IF NOT EXISTS profile_bio_format ON profiles(bio_format_version)")
    connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    version = connection.execute("SELECT value FROM metadata WHERE key = 'request_id_format'").fetchone()
    if version != ("uuid-v4",):
        # Older workers accepted UUIDs that the frontend could not read. Upgrade
        # the cached database once without discarding its incremental-sync cursor.
        with connection:
            invalid = [
                login for login, encoded in connection.execute("SELECT login, result FROM profiles")
                if not valid_request_id(json.loads(encoded).get("requestId"))
            ]
            for login in invalid:
                connection.execute("DELETE FROM profiles WHERE login = ?", (login,))
                print(f"Removed cached profile @{login}: its request ID is not UUID v4.", file=sys.stderr)
            connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('request_id_format', 'uuid-v4')")
    return connection


def decoded_result(comment):
    author = comment.get("user", {})
    if author.get("login") != "github-actions[bot]" or author.get("type") != "Bot":
        return None
    body = comment.get("body")
    match = RESULT_JSON.fullmatch(body) if isinstance(body, str) else None
    if not match:
        return None
    try:
        result = json.loads(match[1])
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) and result.get("status") in ("verified", "excluded") else None


def validate_result(result, request):
    if (
        result.get("status") != "verified"
        or not valid_request_id(result.get("requestId"))
        or result.get("requestId") != request.request_id
        or not isinstance(result.get("login"), str)
        or result["login"].casefold() != request.login.casefold()
        or not is_release_url(result.get("releaseUrl"))
        or any(not isinstance(result.get(field), str) for field in ("name", "bio", "badge", "avatarUrl", "observedAt"))
        or result["badge"] not in ("", "PRO")
    ):
        raise VerificationError("A successful result does not match its public request.")
    avatar = result["avatarUrl"]
    if avatar and (
        not is_avatar_url(avatar)
        or any(key not in ("s", "v") or not re.fullmatch(r"[0-9]+", value) for key, value in parse_qsl(urlsplit(avatar).query, keep_blank_values=True))
    ):
        raise VerificationError("A successful result contains an invalid avatar URL.")
    try:
        observed_at = datetime.fromisoformat(result["observedAt"].replace("Z", "+00:00"))
    except ValueError as error:
        raise VerificationError("A successful result has an invalid observation date.") from error
    if observed_at.tzinfo is None:
        raise VerificationError("The observation date must include a timezone.")
    return observed_at.astimezone(timezone.utc).isoformat(timespec="seconds")


def store_result(connection, result, request):
    observed_at = validate_result(result, request)
    if type(result.get("profilePageStatus")) is not int or result["profilePageStatus"] != 404:
        raise VerificationError("Only a profile with a confirmed HTTP 404 can be indexed.")
    record = {**result, "issueNumber": request.issue_number, "login": request.login}
    connection.execute("""
        INSERT INTO profiles(login, observed_at, issue_number, bio_format_version, result) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(login) DO UPDATE SET
            observed_at = excluded.observed_at,
            issue_number = excluded.issue_number,
            bio_format_version = excluded.bio_format_version,
            result = excluded.result
        WHERE excluded.observed_at >= profiles.observed_at
    """, (request.login, observed_at, request.issue_number, 2 if result.get("bioFormatVersion") == 2 else 1, json.dumps(record, ensure_ascii=False)))


def sync_index(connection, client, now=None):
    # Advance the cursor only after the entire batch commits. The one-second overlap
    # covers GitHub timestamps rounded to seconds; primary-key upserts are idempotent.
    started_at = now or datetime.now(timezone.utc)
    previous = connection.execute("SELECT value FROM metadata WHERE key = 'last_sync'").fetchone()
    since = "" if previous is None else f"&since={previous[0]}"
    requests = {}
    page_statuses = {}
    with connection:
        page = 1
        while True:
            comments = client.request("GET", f"/issues/comments?sort=updated&direction=asc&per_page=100&page={page}{since}")
            if not isinstance(comments, list):
                raise VerificationError("GitHub returned an invalid comment collection.")
            for comment in comments:
                result = decoded_result(comment)
                if result is None:
                    continue
                issue_url = comment.get("issue_url")
                match = re.fullmatch(re.escape(API_ROOT) + r"/issues/([1-9][0-9]*)", issue_url) if isinstance(issue_url, str) else None
                if not match:
                    raise VerificationError("A result comment does not belong to the fixed repository.")
                number = int(match[1])
                if number not in requests:
                    issue = client.request("GET", f"/issues/{number}")
                    try:
                        requests[number] = parse_request(issue)
                    except VerificationError as error:
                        # Issue authors can edit requests after a result is posted.
                        # Invalid mutable input must not block unrelated results.
                        requests[number] = None
                        print(f"Skipping results for issue #{number}: {error}", file=sys.stderr)
                request = requests[number]
                if request is None:
                    continue
                if (
                    result.get("requestId") != request.request_id
                    or isinstance(result.get("login"), str) and result["login"].casefold() != request.login.casefold()
                ):
                    print(f"Skipping result for issue #{number}: the current request no longer matches this result.", file=sys.stderr)
                    continue
                if result.get("status") == "excluded":
                    # Exclusion events remove accounts that have become visible again.
                    if (
                        result.get("requestId") != request.request_id
                        or not isinstance(result.get("login"), str)
                        or result["login"].casefold() != request.login.casefold()
                        or not is_release_url(result.get("releaseUrl"))
                        or type(result.get("profilePageStatus")) is not int
                        or not 200 <= result["profilePageStatus"] < 500
                        or result["profilePageStatus"] in (403, 404, 429)
                    ):
                        raise VerificationError("An excluded result does not match its public request.")
                    connection.execute("DELETE FROM profiles WHERE login = ?", (request.login,))
                    continue
                validate_result(result, request)
                key = request.login.casefold()
                if key not in page_statuses:
                    page_statuses[key] = profile_page_status(request.login)
                status = page_statuses[key]
                if status == 404:
                    store_result(connection, {**result, "profilePageStatus": status}, request)
                else:
                    connection.execute("DELETE FROM profiles WHERE login = ?", (request.login,))
            if len(comments) < 100:
                break
            page += 1
        cursor = (started_at - timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('last_sync', ?)", (cursor,))


def export_index(connection, output):
    snapshot_id = str(uuid4())
    count = connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    snapshot = output / "profiles"
    page_directory = snapshot / "pages"
    login_directory = snapshot / "users"
    page_directory.mkdir(parents=True, exist_ok=True)
    login_directory.mkdir(parents=True, exist_ok=True)
    stale_pages = set(page_directory.glob("*.json"))
    stale_logins = set(login_directory.glob("*.json"))

    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

    page = []
    page_number = 1
    for login, encoded in connection.execute("SELECT login, result FROM profiles ORDER BY observed_at DESC, login"):
        result = {**json.loads(encoded), "snapshotId": snapshot_id}
        login_path = login_directory / f"{login.casefold()}.json"
        write(login_path, result)
        stale_logins.discard(login_path)
        page.append(result)
        if len(page) == PAGE_SIZE:
            page_path = page_directory / f"{page_number}.json"
            write(page_path, page)
            stale_pages.discard(page_path)
            page_number += 1
            page = []
    if page or count == 0:
        page_path = page_directory / f"{page_number}.json"
        write(page_path, page)
        stale_pages.discard(page_path)
    for path in stale_pages | stale_logins:
        path.unlink()
    write(snapshot / "index.json", {
        "snapshotId": snapshot_id, "total": count, "pageSize": PAGE_SIZE,
        "pageCount": max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE),
    })
    return count


def refresh_bios(connection):
    from scripts.verify_profile import ProfileRequest, make_result, observe_profile

    pending = [login for (login,) in connection.execute("SELECT login FROM profiles WHERE bio_format_version < 2")]
    for login in pending:
        encoded = connection.execute("SELECT result FROM profiles WHERE login = ?", (login,)).fetchone()[0]
        result = json.loads(encoded)
        if profile_page_status(result["login"]) != 404:
            with connection:
                connection.execute("DELETE FROM profiles WHERE login = ?", (result["login"],))
            continue
        # Migration reads an already visible contributor. It never edits the Release.
        observation = observe_profile(result["login"], source_url=result["releaseUrl"])
        if observation["status"] != "verified":
            continue
        request = ProfileRequest(result["issueNumber"], result["requestId"], result["login"])
        with connection:
            store_result(connection, make_result(request, {**observation, "profilePageStatus": 404}), request)


def needs_bio_refresh(connection):
    return connection.execute("SELECT EXISTS(SELECT 1 FROM profiles WHERE bio_format_version < 2)").fetchone()[0] == 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--refresh-bios", action="store_true")
    args = parser.parse_args()
    with closing(open_index(args.database)) as connection:
        if args.sync:
            sync_index(connection, GitHubClient(os.environ.get("GITHUB_TOKEN")))
        if args.refresh_bios:
            refresh_bios(connection)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"needs_bio_refresh={str(needs_bio_refresh(connection)).lower()}\n")
        count = export_index(connection, args.output)
    print(f"Exported {count} verified profiles in pages of {PAGE_SIZE}.")


if __name__ == "__main__":
    main()
