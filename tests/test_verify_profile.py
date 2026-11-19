import json
import unittest
from unittest.mock import Mock, patch

from scripts.verify_profile import (
    MENTIONS_END,
    MENTIONS_START,
    RELEASE_URL,
    REPOSITORY,
    REQUEST_MARKER,
    ProfileRequest,
    VerificationError,
    add_mention,
    is_avatar_url,
    is_profile_url,
    load_request,
    make_result,
    parse_request,
    previous_result,
    result_body,
    run_verification,
    update_release,
)


REQUEST_ID = "95b7e76a-df4b-4d91-b507-bcf5bed45e44"


def issue(login="rD227", body=None):
    return {
        "number": 42,
        "title": f"Verify profile: {login}",
        "body": body if body is not None else f"{REQUEST_MARKER}\nRequest ID: {REQUEST_ID}\n",
    }


def comment(request, author="github-actions[bot]", kind="Bot", status="verified"):
    result = make_result(request, {"status": status, "message": "Observed publicly."})
    return {"id": 100, "user": {"login": author, "type": kind}, "body": result_body(result)}


class RequestTests(unittest.TestCase):
    def test_accepts_normal_ascii_login_and_canonical_uuid(self):
        request = parse_request(issue("some-user"))
        self.assertEqual(request, ProfileRequest(42, REQUEST_ID, "some-user"))

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


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.request = parse_request(issue())

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


if __name__ == "__main__":
    unittest.main()
