from io import BytesIO
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from scripts.verify_profile import (
    BATCH_MARKER,
    MENTIONS_END,
    MENTIONS_START,
    RELEASE_URL,
    REPOSITORY,
    REQUEST_MARKER,
    NoProfileRedirect,
    ProfileRequest,
    VerificationError,
    add_mention,
    create_profile_release,
    is_avatar_url,
    is_profile_url,
    is_release_url,
    load_request,
    make_result,
    observe_profile,
    parse_request,
    previous_result,
    profile_page_status,
    read_hovercard,
    release_catalog,
    release_members,
    result_body,
    run_verification,
    update_release,
    valid_request_id,
)


REQUEST_ID = "95b7e76a-df4b-4d91-b507-bcf5bed45e44"


def issue(login="rD227", body=None):
    return {
        "number": 42,
        "title": f"Verify profile: {login}",
        "body": body if body is not None else f"{REQUEST_MARKER}\nRequest ID: {REQUEST_ID}\n",
    }


def comment(request, author="github-actions[bot]", kind="Bot", status="verified"):
    result = make_result(request, {"status": status, "message": "Observed publicly.", "profilePageStatus": 404})
    return {"id": 100, "user": {"login": author, "type": kind}, "body": result_body(result)}


def release(tag="v0.1.1", logins=()):
    body = "" if tag == "v0.1.1" else BATCH_MARKER
    for login in logins:
        body = add_mention(body, login)
    return {"id": int(tag.rsplit(".", 1)[1]), "tag_name": tag, "draft": False,
            "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}", "body": body}


class MemoryReleaseClient:
    def __init__(self, releases):
        self.releases = {item["tag_name"]: dict(item) for item in releases}
        self.request = Mock(side_effect=self.respond)

    def respond(self, method, path, payload=None, allow_missing=False):
        if method == "GET" and path.startswith("/releases?"):
            return [dict(item) for item in self.releases.values()]
        if method == "GET" and path.startswith("/releases/tags/"):
            return dict(self.releases[path.rsplit("/", 1)[1]])
        if method == "GET" and path.startswith("/git/ref/tags/"):
            return None
        if method == "POST" and path == "/releases":
            created = {**payload, "id": max(item["id"] for item in self.releases.values()) + 1,
                       "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{payload['tag_name']}"}
            self.releases[created["tag_name"]] = created
            return dict(created)
        if method == "PATCH" and path.startswith("/releases/"):
            current = next(item for item in self.releases.values() if item["id"] == int(path.rsplit("/", 1)[1]))
            current.update(payload)
            return dict(current)
        raise AssertionError((method, path))


class RequestTests(unittest.TestCase):
    def test_accepts_normal_ascii_login_and_canonical_uuid(self):
        request = parse_request(issue("some-user"))
        self.assertEqual(request, ProfileRequest(42, REQUEST_ID, "some-user"))

    def test_uppercase_v4_id_is_normalized_and_other_versions_or_variants_are_rejected(self):
        body = f"{REQUEST_MARKER}\nRequest ID: {REQUEST_ID.upper()}\n"
        self.assertEqual(parse_request(issue(body=body)).request_id, REQUEST_ID)
        self.assertTrue(valid_request_id(REQUEST_ID.upper()))
        for value in (
            "95b7e76a-df4b-1d91-b507-bcf5bed45e44",
            "95b7e76a-df4b-5d91-b507-bcf5bed45e44",
            "95b7e76a-df4b-4d91-7507-bcf5bed45e44",
            "95b7e76a-df4b-4d91-c507-bcf5bed45e44",
            "00000000-0000-0000-0000-000000000000",
        ):
            with self.subTest(value=value):
                self.assertFalse(valid_request_id(value))
                with self.assertRaisesRegex(VerificationError, "UUID v4"):
                    parse_request(issue(body=f"{REQUEST_MARKER}\nRequest ID: {value}\n"))

    def test_rejects_injection_and_invalid_github_logins(self):
        for login in (
            "$(touch pwned)", "user; echo secret", "user\n@other", "user/other", "user'",
            "用户", "ａｂｃ", "-user", "user-", "two--hyphens", "a" * 40, "", "user ",
        ):
            with self.subTest(login=login), self.assertRaises(VerificationError):
                parse_request(issue(login))

    def test_rejects_missing_or_duplicate_request_markers_and_ids(self):
        for body in (
            f"Request ID: {REQUEST_ID}",
            f"{REQUEST_MARKER}\nRequest ID: nope",
            f"{REQUEST_MARKER}\nRequest ID: {REQUEST_ID}\nRequest ID: {REQUEST_ID}",
            f"{REQUEST_MARKER}{REQUEST_MARKER}\nRequest ID: {REQUEST_ID}",
            f"{REQUEST_MARKER}\nRequest ID: {REQUEST_ID} extra",
            f"{REQUEST_MARKER}\nRequest ID: {REQUEST_ID}\nRequest ID: malformed",
        ):
            with self.subTest(body=body), self.assertRaises(VerificationError):
                parse_request(issue(body=body))

    def test_rejects_pull_requests_and_non_integer_issue_numbers(self):
        values = [{**issue(), "pull_request": {}}, {**issue(), "number": True}, {**issue(), "number": "42"}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(VerificationError):
                parse_request(value)

    def test_dispatch_only_accepts_numeric_issue_number_in_fixed_repository(self):
        client = Mock()
        client.request.return_value = issue()
        event = {"repository": {"full_name": REPOSITORY}, "inputs": {"issue_number": "42"}}
        self.assertEqual(load_request(event, "workflow_dispatch", REPOSITORY, client).issue_number, 42)
        client.request.assert_called_once_with("GET", "/issues/42")
        for number in ("42; echo secret", "../42", "0", 42):
            event["inputs"]["issue_number"] = number
            with self.subTest(number=number), self.assertRaises(VerificationError):
                load_request(event, "workflow_dispatch", REPOSITORY, client)
        with self.assertRaises(VerificationError):
            load_request(event, "workflow_dispatch", "someone/fork", client)


class ReleaseTests(unittest.TestCase):
    def test_six_account_limit_counts_distinct_logins_and_preserves_existing_slot(self):
        client = Mock()
        client.request.return_value = release(logins=[f"user{i}" for i in range(6)])
        with self.assertRaisesRegex(VerificationError, "six accounts"):
            update_release(client, "seventh")
        client.request.assert_called_once()
        client.reset_mock()
        update_release(client, "USER0")
        client.request.assert_called_once()
        self.assertEqual(len(release_members(client.request.return_value)), 6)

    def test_catalog_sorts_numeric_versions_and_reserves_unrelated_releases(self):
        client = Mock()
        other = {**release("v0.1.11"), "body": "CLI release"}
        client.request.return_value = [release(), release("v0.1.2"), release("v0.1.10"), other]
        managed, occupied = release_catalog(client)
        self.assertEqual([item["tag_name"] for item in managed], ["v0.1.10", "v0.1.2", "v0.1.1"])
        self.assertEqual(occupied, {1, 2, 10, 11})

    def test_create_increments_patch_and_keeps_cli_latest_unchanged(self):
        client = Mock()
        client.request.side_effect = [None, release("v0.1.2")]
        created = create_profile_release(client, {1})
        self.assertEqual(created["tag_name"], "v0.1.2")
        method, path, payload = client.request.call_args.args
        self.assertEqual((method, path), ("POST", "/releases"))
        self.assertEqual(payload["tag_name"], "v0.1.2")
        self.assertEqual(payload["make_latest"], "false")
        self.assertIn(BATCH_MARKER, payload["body"])
        self.assertEqual(release_members(created), set())
        self.assertNotIn(MENTIONS_START, payload["body"])

    def test_create_skips_existing_tag_without_overwriting_it(self):
        client = Mock()
        client.request.side_effect = [{"ref": "refs/tags/v0.1.2"}, None, release("v0.1.3")]
        self.assertEqual(create_profile_release(client, {1})["tag_name"], "v0.1.3")

    def test_source_url_accepts_managed_version_series_only(self):
        self.assertTrue(is_release_url(release("v0.1.10")["html_url"]))
        for url in (RELEASE_URL + "?bad=1", RELEASE_URL.replace(REPOSITORY, "other/repo"), RELEASE_URL.replace("v0.1.1", "v0.1.0")):
            self.assertFalse(is_release_url(url))

    def test_preserves_release_notes_and_adds_owned_section(self):
        original = "# v0.1.1\r\n\r\nOriginal notes, links, and @other-user.\r\n"
        updated = add_mention(original, "rD227")
        self.assertTrue(updated.startswith(original))
        self.assertEqual(updated.count(MENTIONS_START), 1)
        self.assertEqual(updated.count(MENTIONS_END), 1)
        self.assertIn("\n@rD227\n", updated)

    def test_mentions_are_case_insensitively_idempotent(self):
        body = add_mention("Original notes", "rD227")
        self.assertEqual(add_mention(body, "RD227"), body)
        appended = add_mention(body, "xusu616")
        self.assertIn("\n@rD227\n@xusu616\n", appended)
        self.assertEqual(add_mention(appended, "xusu616"), appended)

    def test_preserves_content_after_owned_section(self):
        body = add_mention("before", "rD227") + "\n\n## Keep this\n@outside"
        updated = add_mention(body, "xusu616")
        self.assertTrue(updated.startswith("before"))
        self.assertTrue(updated.endswith("\n\n## Keep this\n@outside"))

    def test_malformed_owned_section_is_an_error_without_overwriting(self):
        for body in (
            MENTIONS_START,
            MENTIONS_END,
            MENTIONS_END + MENTIONS_START,
            MENTIONS_START + MENTIONS_START + MENTIONS_END,
        ):
            with self.subTest(body=body), self.assertRaises(VerificationError):
                add_mention(body, "rD227")

    def test_only_patch_fixed_release_body_and_skip_duplicate(self):
        client = Mock()
        client.request.return_value = {
            "id": 123, "tag_name": "v0.1.1", "draft": False,
            "html_url": RELEASE_URL, "body": "Original notes",
        }
        update_release(client, "rD227")
        self.assertEqual(client.request.call_count, 2)
        method, path, payload = client.request.call_args.args
        self.assertEqual((method, path), ("PATCH", "/releases/123"))
        self.assertEqual(set(payload), {"body"})
        self.assertTrue(payload["body"].startswith("Original notes"))
        client.request.reset_mock()
        client.request.return_value["body"] = payload["body"]
        update_release(client, "RD227")
        self.assertEqual(client.request.call_count, 1)

    def test_refuses_draft_or_redirected_release(self):
        for overrides in ({"draft": True}, {"tag_name": "v2"}, {"html_url": "https://example.com"}):
            client = Mock()
            client.request.return_value = {
                "id": 123, "tag_name": "v0.1.1", "draft": False, "html_url": RELEASE_URL, **overrides,
            }
            with self.subTest(overrides=overrides), self.assertRaises(VerificationError):
                update_release(client, "rD227")
            self.assertEqual(client.request.call_count, 1)


class ProfilePageTests(unittest.TestCase):
    def test_checks_original_profile_url_without_authentication_or_redirects(self):
        response = Mock(status=200)
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch("scripts.verify_profile.build_opener", return_value=opener) as build:
            self.assertEqual(profile_page_status("rD227"), 200)
        handler = build.call_args.args[0]
        self.assertIsInstance(handler, NoProfileRedirect)
        self.assertIsNone(handler.redirect_request(None, None, None, None, None, None))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://github.com/rD227")
        self.assertEqual(request.get_method(), "GET")
        self.assertFalse(request.has_header("Authorization"))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)

    def test_returns_explicit_404_and_original_redirect_status(self):
        for status in (404, 301, 302, 307, 308):
            body = BytesIO(b"not needed")
            error = HTTPError("https://github.com/rD227", status, "status", {}, body)
            with self.subTest(status=status), patch("scripts.verify_profile.build_opener") as build:
                build.return_value.open.side_effect = error
                self.assertEqual(profile_page_status("rD227"), status)
                self.assertTrue(body.closed)
                build.return_value.open.assert_called_once()

    def test_rate_limits_and_server_failures_do_not_become_visibility_results(self):
        for status in (403, 429, 500, 502, 503):
            error = HTTPError("https://github.com/rD227", status, "status", {}, BytesIO())
            with self.subTest(status=status), patch("scripts.verify_profile.build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaisesRegex(VerificationError, "could not be confirmed"):
                    profile_page_status("rD227")

    def test_network_failure_is_explicit_and_invalid_login_never_requests_url(self):
        for error in (URLError("unreachable"), TimeoutError("timeout")):
            with self.subTest(error=error), patch("scripts.verify_profile.build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaisesRegex(VerificationError, "visibility is unknown"):
                    profile_page_status("rD227")
        with patch("scripts.verify_profile.build_opener") as build:
            with self.assertRaises(VerificationError):
                profile_page_status("../not-a-login")
            build.assert_not_called()


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.request = parse_request(issue())
        status_patch = patch("scripts.verify_profile.profile_page_status", return_value=404)
        self.page_status = status_patch.start()
        self.addCleanup(status_patch.stop)

    def test_accepts_exact_public_profile_links_only(self):
        self.assertTrue(is_profile_url("/rd227", "rD227"))
        self.assertTrue(is_profile_url("https://github.com/rD227", "rD227"))
        for url in ("https://example.com/rD227", "//evil.test/rD227", "/rD227/repo", "/rD227?q=x", "javascript:alert(1)"):
            with self.subTest(url=url):
                self.assertFalse(is_profile_url(url, "rD227"))

    def test_avatar_matches_the_frontend_public_numeric_avatar_contract(self):
        self.assertTrue(is_avatar_url("https://avatars.githubusercontent.com/u/170849438?s=64&v=4"))
        self.assertTrue(is_avatar_url("https://avatars.githubusercontent.com/in/123?v=4"))
        for url in (
            "https://avatars.githubusercontent.com/anything.svg", "https://example.com/u/123",
            "http://avatars.githubusercontent.com/u/123", "https://avatars.githubusercontent.com/u/user",
            "https://avatars.githubusercontent.com/u/123#fragment", None,
        ):
            with self.subTest(url=url):
                self.assertFalse(is_avatar_url(url))

    def test_bio_cannot_close_fence_or_inject_markdown_html(self):
        bio = "```\n<img src=x onerror=alert(1)>\n@someone & [click](javascript:alert(1))"
        result = make_result(self.request, {"status": "verified", "message": "Observed.", "bio": bio})
        body = result_body(result)
        self.assertEqual(body.count("```"), 2)
        self.assertNotIn("<img", body)
        decoded = json.loads(body.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        self.assertEqual(decoded["bio"], bio)

    def test_hovercard_keeps_newlines_before_result_serialization(self):
        card = Mock()
        identity, bio_region, avatar_region, badge_region = Mock(), Mock(), Mock(), Mock()
        card.get_by_role.side_effect = [identity, bio_region, avatar_region, badge_region]
        login_link = identity.locator.return_value.first
        login_link.get_attribute.return_value = "/rD227"
        login_link.inner_text.return_value = "rD227"
        bio_region.count.return_value = 1
        bio_region.inner_text.return_value = "first second third"
        bio_region.text_content.return_value = "first\nsecond\nthird"
        avatar_region.locator.return_value.first.get_attribute.return_value = "https://avatars.githubusercontent.com/u/170849438?v=4"
        observation = read_hovercard(card, "rD227")
        self.assertEqual(observation["bio"], "first\nsecond\nthird")
        bio_region.inner_text.assert_not_called()
        result = make_result(self.request, {"status": "verified", "message": "Observed.", **observation})
        body = result_body(result)
        decoded = json.loads(body.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        self.assertEqual(decoded["bio"], observation["bio"])

    def test_only_accepts_exact_bot_result_and_request_match(self):
        trusted = comment(self.request)
        forged = comment(self.request, author="someone", kind="User")
        impersonated = comment(self.request, kind="User")
        self.assertEqual(previous_result([forged, impersonated], self.request), (None, None))
        previous, result = previous_result([trusted, forged], self.request)
        self.assertEqual(previous["id"], trusted["id"])
        self.assertEqual(result["status"], "verified")
        other = ProfileRequest(42, "a56ed762-0b94-448e-b20e-7985680b8d8f", "rD227")
        self.assertEqual(previous_result([trusted], other), (None, None))

    def test_completed_request_does_not_edit_release_or_hover_again(self):
        client = Mock()
        client.comments.return_value = [comment(self.request)]
        with patch("scripts.verify_profile.update_release") as update, patch("scripts.verify_profile.observe_profile") as observe:
            self.assertEqual(run_verification(client, self.request), 0)
        update.assert_not_called()
        observe.assert_not_called()
        client.request.assert_called_once_with("PATCH", "/issues/42", {"state": "closed"})

    def test_legacy_success_is_reobserved_to_recover_bio_newlines(self):
        client = Mock()
        legacy = comment(self.request)
        result = make_result(self.request, {"status": "verified", "message": "Old flattened bio."})
        result.pop("bioFormatVersion")
        legacy["body"] = result_body(result)
        client.comments.return_value = [legacy]
        observation = {"status": "verified", "bio": "first\nsecond", "message": "Reobserved."}
        with patch("scripts.verify_profile.observe_profile", return_value=observation) as observe:
            self.assertEqual(run_verification(client, self.request), 0)
        observe.assert_called_once_with(self.request.login, client)
        self.assertEqual(client.request.call_args_list[0].args[:2], ("PATCH", "/issues/comments/100"))
        self.assertIn('"bioFormatVersion": 2', client.request.call_args_list[0].args[2]["body"])

    def test_explicit_refresh_reobserves_current_success(self):
        client = Mock()
        client.comments.return_value = [comment(self.request)]
        with patch("scripts.verify_profile.observe_profile", return_value={"status": "verified", "message": "Refreshed."}) as observe:
            self.assertEqual(run_verification(client, self.request, refresh=True), 0)
        observe.assert_called_once_with(self.request.login, client)

    def test_non404_excludes_even_previous_success_and_skips_release_work(self):
        for status in (200, 301, 302):
            with self.subTest(status=status):
                self.page_status.return_value = status
                client = Mock()
                client.comments.return_value = [comment(self.request)]
                with patch("scripts.verify_profile.observe_profile") as observe, patch("scripts.verify_profile.update_release") as update:
                    self.assertEqual(run_verification(client, self.request), 0)
                observe.assert_not_called()
                update.assert_not_called()
                decoded = json.loads(client.request.call_args_list[0].args[2]["body"].split("```json\n", 1)[1].rsplit("\n```", 1)[0])
                self.assertEqual(decoded["status"], "excluded")
                self.assertEqual(decoded["profilePageStatus"], status)

    def test_verified_result_records_explicit_404_and_rechecks_old_evidence(self):
        client = Mock()
        legacy = comment(self.request)
        old = make_result(self.request, {"status": "verified", "message": "Old result."})
        old.pop("profilePageStatus")
        legacy["body"] = result_body(old)
        client.comments.return_value = [legacy]
        with patch("scripts.verify_profile.observe_profile", return_value={"status": "verified", "message": "Observed."}) as observe:
            self.assertEqual(run_verification(client, self.request), 0)
        observe.assert_called_once_with(self.request.login, client)
        self.page_status.assert_called_once_with(self.request.login)
        self.assertIn('"profilePageStatus": 404', client.request.call_args_list[0].args[2]["body"])

    def test_unknown_page_status_fails_job_without_reusing_cached_success(self):
        self.page_status.side_effect = VerificationError("The profile page could not be checked.")
        client = Mock()
        client.comments.return_value = [comment(self.request)]
        with patch("scripts.verify_profile.observe_profile") as observe, patch("scripts.verify_profile.traceback.print_exception"):
            self.assertEqual(run_verification(client, self.request), 1)
        observe.assert_not_called()
        self.assertIn('"status": "error"', client.request.call_args_list[0].args[2]["body"])

    def test_error_posts_explicit_result_closes_request_and_fails_job(self):
        client = Mock()
        client.comments.return_value = []
        with patch("scripts.verify_profile.update_release"), patch("scripts.verify_profile.observe_profile", side_effect=RuntimeError("secret credentials")), patch("scripts.verify_profile.traceback.print_exception") as diagnostic:
            self.assertEqual(run_verification(client, self.request), 1)
        self.assertFalse(diagnostic.call_args.kwargs["chain"])
        method, path, payload = client.request.call_args_list[0].args
        self.assertEqual((method, path), ("POST", "/issues/42/comments"))
        self.assertIn('"status": "error"', payload["body"])
        self.assertNotIn("secret credentials", payload["body"])
        self.assertEqual(client.request.call_args_list[-1].args, ("PATCH", "/issues/42", {"state": "closed"}))

    def test_retry_updates_own_error_comment_and_keeps_request_correlation(self):
        client = Mock()
        client.comments.return_value = [comment(self.request, status="error")]
        observation = {"status": "verified", "name": "Visible name", "bio": "Visible bio", "message": "Observed."}
        with patch("scripts.verify_profile.update_release"), patch("scripts.verify_profile.observe_profile", return_value=observation):
            self.assertEqual(run_verification(client, self.request), 0)
        method, path, payload = client.request.call_args_list[0].args
        self.assertEqual((method, path), ("PATCH", "/issues/comments/100"))
        result = json.loads(payload["body"].split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        self.assertEqual(result["requestId"], REQUEST_ID)
        self.assertEqual(result["login"], "rD227")
        self.assertEqual(result["status"], "verified")


class ObserveTests(unittest.TestCase):
    def setUp(self):
        catalog_patch = patch("scripts.verify_profile.release_catalog", return_value=([release()], {1}))
        self.catalog = catalog_patch.start()
        self.addCleanup(catalog_patch.stop)

    def browser(self, hrefs):
        page = Mock()
        page.url = RELEASE_URL
        page.goto.return_value.status = 200
        links = page.locator.return_value.filter.return_value.locator.return_value
        links.count.return_value = len(hrefs)
        entries = []
        for href in hrefs:
            link = Mock()
            link.get_attribute.return_value = href
            entries.append(link)
        links.nth.side_effect = lambda index: entries[index]
        playwright = Mock()
        playwright.chromium.launch.return_value.new_page.return_value = page
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        module = types.ModuleType("playwright.sync_api")
        module.TimeoutError = TimeoutError
        module.sync_playwright = Mock(return_value=manager)
        return page, module

    def test_existing_contributor_is_read_before_any_release_edit(self):
        page, module = self.browser(["/rD227"])
        with patch.dict(sys.modules, {"playwright.sync_api": module}), patch("scripts.verify_profile.update_release") as update, patch("scripts.verify_profile.read_hovercard", return_value={"bio": "first\nsecond"}):
            result = observe_profile("rD227", Mock())
        self.assertEqual(result["status"], "verified")
        update.assert_not_called()
        page.goto.assert_called_once()

    def test_missing_contributor_edits_once_after_initial_read(self):
        page, module = self.browser([])
        order = []
        page.goto.side_effect = lambda *args, **kwargs: order.append("read") or Mock(status=200)
        with patch.dict(sys.modules, {"playwright.sync_api": module}), patch("scripts.verify_profile.update_release", side_effect=lambda *args: order.append("edit")) as update:
            result = observe_profile("missing-user", Mock())
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(order[:2], ["read", "edit"])
        self.assertEqual(update.call_count, 1)

    def test_seventh_account_allocates_next_release_and_records_actual_source(self):
        full = release(logins=[f"user{i}" for i in range(6)])
        self.catalog.return_value = ([full], {1})
        page, module = self.browser([])
        created = release("v0.1.2", ["seventh"])
        def navigate(url, **kwargs):
            page.url = url
            return Mock(status=200)
        page.goto.side_effect = navigate
        client = Mock()
        with patch.dict(sys.modules, {"playwright.sync_api": module}), patch("scripts.verify_profile.create_profile_release", return_value=created) as create, patch("scripts.verify_profile.update_release", return_value=False) as update:
            result = observe_profile("seventh", client)
        create.assert_called_once()
        update.assert_called_once_with(client, "seventh", "v0.1.2")
        self.assertEqual(result["releaseUrl"], created["html_url"])
        self.assertEqual(page.goto.call_args.args[0], created["html_url"])

    def test_existing_account_reuses_full_release_and_reads_its_link(self):
        full = release("v0.1.2", ["rD227", *[f"user{i}" for i in range(5)]])
        self.catalog.return_value = ([full, release()], {1, 2})
        page, module = self.browser(["/rD227"])
        page.url = full["html_url"]
        with patch.dict(sys.modules, {"playwright.sync_api": module}), patch("scripts.verify_profile.create_profile_release") as create, patch("scripts.verify_profile.update_release") as update, patch("scripts.verify_profile.read_hovercard", return_value={"bio": "first\nsecond"}):
            result = observe_profile("rD227", Mock())
        create.assert_not_called()
        update.assert_not_called()
        self.assertEqual(result["releaseUrl"], full["html_url"])
        page.goto.assert_called_once()

    def test_sixth_account_uses_current_release_without_creating_another(self):
        self.catalog.return_value = ([release(logins=[f"user{i}" for i in range(5)])], {1})
        page, module = self.browser([])
        with patch.dict(sys.modules, {"playwright.sync_api": module}), patch("scripts.verify_profile.create_profile_release") as create, patch("scripts.verify_profile.update_release", return_value=False) as update:
            result = observe_profile("sixth", Mock())
        create.assert_not_called()
        self.assertEqual(update.call_args.args[1:3], ("sixth", "v0.1.1"))
        self.assertEqual(result["releaseUrl"], RELEASE_URL)

    def test_visible_contributors_also_count_toward_capacity(self):
        page, module = self.browser([f"/user{i}" for i in range(6)])
        created = release("v0.1.2", ["seventh"])
        def navigate(url, **kwargs):
            page.url = url
            return Mock(status=200)
        page.goto.side_effect = navigate
        client = Mock()
        with patch.dict(sys.modules, {"playwright.sync_api": module}), patch("scripts.verify_profile.create_profile_release", return_value=created) as create, patch("scripts.verify_profile.update_release", return_value=False) as update:
            result = observe_profile("seventh", client)
        create.assert_called_once()
        update.assert_called_once_with(client, "seventh", "v0.1.2")
        self.assertEqual(result["releaseUrl"], created["html_url"])

    def test_seven_failed_queries_do_not_fill_slots_or_create_another_release(self):
        self.catalog.side_effect = release_catalog
        client = MemoryReleaseClient([release()])
        page, module = self.browser([])
        with patch.dict(sys.modules, {"playwright.sync_api": module}):
            results = [observe_profile(f"missing-user{i}", client) for i in range(7)]
        self.assertTrue(all(result["status"] == "unavailable" for result in results))
        self.assertEqual(list(client.releases), ["v0.1.1"])
        self.assertEqual(release_members(client.releases["v0.1.1"]), set())

    def test_six_successful_queries_keep_slots_and_seventh_gets_new_release(self):
        self.catalog.side_effect = release_catalog
        client = MemoryReleaseClient([release()])
        page, module = self.browser([])
        links = page.locator.return_value.filter.return_value.locator.return_value
        current_login = ""

        def navigate(url, **kwargs):
            page.url = url
            members = release_members(client.releases[url.rsplit("/", 1)[1]])
            links.count.return_value = int(current_login in members)
            link = Mock()
            link.get_attribute.return_value = f"/{current_login}"
            links.nth.side_effect = lambda index: link
            return Mock(status=200)

        page.goto.side_effect = navigate
        results = []
        with patch.dict(sys.modules, {"playwright.sync_api": module}), patch("scripts.verify_profile.read_hovercard", return_value={"bio": "Available"}):
            for index in range(7):
                current_login = f"user{index}"
                results.append(observe_profile(current_login, client))
        self.assertTrue(all(result["status"] == "verified" for result in results))
        self.assertEqual(release_members(client.releases["v0.1.1"]), {f"user{i}" for i in range(6)})
        self.assertEqual(release_members(client.releases["v0.1.2"]), {"user6"})
        self.assertEqual(results[-1]["releaseUrl"], client.releases["v0.1.2"]["html_url"])

    def test_cleanup_preserves_existing_members_notes_and_later_manual_edit(self):
        self.catalog.side_effect = release_catalog
        original = release(logins=["existing-member"])
        original["body"] = "Original notes, thanks to @credited-user.\n\n" + original["body"]
        client = MemoryReleaseClient([original])
        page, module = self.browser([])
        manual_edit = "\nA later manual edit credits @another-user."
        patched = False

        def respond(method, path, payload=None, **kwargs):
            nonlocal patched
            result = client.respond(method, path, payload, **kwargs)
            if method == "PATCH" and not patched:
                patched = True
                client.releases["v0.1.1"]["body"] += manual_edit
            return result

        client.request.side_effect = respond
        with patch.dict(sys.modules, {"playwright.sync_api": module}):
            self.assertEqual(observe_profile("missing-user", client)["status"], "unavailable")
        self.assertEqual(client.releases["v0.1.1"]["body"], original["body"] + manual_edit)

    def test_failed_new_batch_is_empty_and_reused_by_the_next_query(self):
        self.catalog.side_effect = release_catalog
        original = release(logins=[f"user{i}" for i in range(6)])
        client = MemoryReleaseClient([original])
        page, module = self.browser([])

        def navigate(url, **kwargs):
            page.url = url
            return Mock(status=200)

        page.goto.side_effect = navigate
        with patch.dict(sys.modules, {"playwright.sync_api": module}):
            for login in ("missing-one", "missing-two"):
                result = observe_profile(login, client)
                self.assertEqual(result["status"], "unavailable")
                self.assertTrue(result["releaseUrl"].endswith("/v0.1.2"))
        self.assertEqual(list(client.releases), ["v0.1.1", "v0.1.2"])
        self.assertEqual(client.releases["v0.1.1"]["body"], original["body"])
        self.assertEqual(release_members(client.releases["v0.1.2"]), set())

    def test_existing_member_survives_a_temporary_hovercard_timeout(self):
        original = release(logins=["existing-user"])
        self.catalog.return_value = ([original], {1})
        client = MemoryReleaseClient([original])
        page, module = self.browser(["/existing-user"])
        page.get_by_role.return_value.wait_for.side_effect = TimeoutError("Hovercard timed out")
        with patch.dict(sys.modules, {"playwright.sync_api": module}):
            self.assertEqual(observe_profile("existing-user", client)["status"], "unavailable")
        self.assertEqual(client.releases["v0.1.1"]["body"], original["body"])
        client.request.assert_not_called()

    def test_browser_failure_after_reservation_cleans_up_and_preserves_the_error(self):
        self.catalog.side_effect = release_catalog
        client = MemoryReleaseClient([release()])
        page, module = self.browser([])
        error = TimeoutError("Release navigation timed out")
        page.goto.side_effect = [Mock(status=200), error]
        with patch.dict(sys.modules, {"playwright.sync_api": module}), self.assertRaises(TimeoutError) as caught:
            observe_profile("missing-user", client)
        self.assertIs(caught.exception, error)
        self.assertEqual(release_members(client.releases["v0.1.1"]), set())

    def test_patch_applied_before_response_timeout_is_also_cleaned_up(self):
        client = MemoryReleaseClient([release()])
        error = TimeoutError("PATCH response timed out")
        patched = False

        def respond(method, path, payload=None, **kwargs):
            nonlocal patched
            result = client.respond(method, path, payload, **kwargs)
            if method == "PATCH" and not patched:
                patched = True
                raise error
            return result

        client.request.side_effect = respond
        with self.assertRaises(TimeoutError) as caught:
            update_release(client, "missing-user")
        self.assertIs(caught.exception, error)
        self.assertEqual(release_members(client.releases["v0.1.1"]), set())

    def test_operation_and_cleanup_failures_are_both_reported(self):
        client = MemoryReleaseClient([release()])
        operation_error = TimeoutError("PATCH response timed out")
        cleanup_error = VerificationError("Cleanup read could not reach GitHub.")
        patched = False

        def respond(method, path, payload=None, **kwargs):
            nonlocal patched
            if patched:
                raise cleanup_error
            result = client.respond(method, path, payload, **kwargs)
            if method == "PATCH":
                patched = True
                raise operation_error
            return result

        client.request.side_effect = respond
        with self.assertRaises(ExceptionGroup) as caught:
            update_release(client, "missing-user")
        self.assertEqual(caught.exception.exceptions, (operation_error, cleanup_error))


if __name__ == "__main__":
    unittest.main()
