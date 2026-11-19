from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts.profile_index import export_index, open_index, refresh_bios, store_result, sync_index
from scripts.verify_profile import API_ROOT, RELEASE_URL, REQUEST_MARKER, ProfileRequest, VerificationError, make_result, result_body


REQUEST_ID = "95b7e76a-df4b-4d91-b507-bcf5bed45e44"


def request(login="rD227", number=42):
    return ProfileRequest(number, REQUEST_ID, login)


def result(login="rD227", observed_at="2026-11-19T01:00:00Z"):
    profile = make_result(request(login), {"status": "verified", "name": login, "bio": "first\nsecond", "badge": "", "avatarUrl": "", "message": "Observed.", "profilePageStatus": 404})
    profile["observedAt"] = observed_at
    return profile


def issue():
    return {"number": 42, "title": "Verify profile: rD227", "body": f"{REQUEST_MARKER}\nRequest ID: {REQUEST_ID}\n"}


def comment(profile=None):
    return {"user": {"login": "github-actions[bot]", "type": "Bot"}, "issue_url": f"{API_ROOT}/issues/42", "body": result_body(profile or result())}


class IndexTests(unittest.TestCase):
    def setUp(self):
        status_patch = patch("scripts.profile_index.profile_page_status", return_value=404)
        self.page_status = status_patch.start()
        self.addCleanup(status_patch.stop)

    def test_username_and_date_queries_use_indexes(self):
        with closing(open_index(":memory:")) as connection:
            login_plan = connection.execute("EXPLAIN QUERY PLAN SELECT result FROM profiles WHERE login = ?", ("rD227",)).fetchone()[3]
            date_plan = connection.execute("EXPLAIN QUERY PLAN SELECT result FROM profiles ORDER BY observed_at DESC, login").fetchone()[3]
            self.assertIn("INDEX", login_plan)
            self.assertIn("profile_date", date_plan)

    def test_login_primary_key_is_case_insensitive_and_keeps_latest_success(self):
        with closing(open_index(":memory:")) as connection:
            store_result(connection, result(), request())
            store_result(connection, result("RD227", "2026-11-19T02:00:00Z"), request("RD227"))
            store_result(connection, result("rd227", "2026-11-19T00:00:00Z"), request("rd227"))
            rows = connection.execute("SELECT result FROM profiles").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0])["observedAt"], "2026-11-19T02:00:00Z")

    def test_rejects_failed_mismatched_or_invalid_result(self):
        with closing(open_index(":memory:")) as connection:
            for change in ({"status": "unavailable"}, {"login": "other"}, {"requestId": "other"}, {"badge": "invalid"}, {"observedAt": "2026-11-19"}, {"avatarUrl": "https://avatars.githubusercontent.com/u/1?bad=1"}, {"profilePageStatus": 200}, {"profilePageStatus": None}, {"profilePageStatus": 404.0}):
                with self.subTest(change=change), self.assertRaises(VerificationError):
                    store_result(connection, {**result(), **change}, request())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)

    def test_exports_small_pages_and_direct_username_file_with_newlines(self):
        with closing(open_index(":memory:")) as connection, TemporaryDirectory() as directory:
            for index in range(13):
                login = f"user{index}"
                store_result(connection, result(login, f"2026-11-19T01:{index:02}:00Z"), request(login, index + 1))
            self.assertEqual(export_index(connection, Path(directory)), 13)
            root = Path(directory) / "profiles"
            self.assertEqual(json.loads((root / "index.json").read_text()), {"total": 13, "pageSize": 6, "pageCount": 3})
            self.assertEqual([len(json.loads((root / "pages" / f"{page}.json").read_text())) for page in (1, 2, 3)], [6, 6, 1])
            profile = json.loads((root / "users" / "user12.json").read_text())
            self.assertEqual(profile["bio"], "first\nsecond")
            self.assertEqual(profile["issueNumber"], 13)

    def test_sync_reads_only_new_comments_and_checks_each_request_once(self):
        with closing(open_index(":memory:")) as connection:
            client = Mock()
            forged = {**comment(), "user": {"login": "someone", "type": "User"}}
            client.request.side_effect = [[comment(), comment(), forged], issue()]
            sync_index(connection, client, datetime(2026, 11, 19, 2, tzinfo=timezone.utc))
            self.assertEqual(client.request.call_count, 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 1)
            self.page_status.assert_called_once_with("rD227")
            client.reset_mock()
            client.request.side_effect = [[]]
            sync_index(connection, client, datetime(2026, 11, 19, 3, tzinfo=timezone.utc))
            self.assertIn("&since=2026-11-19T01:59:59Z", client.request.call_args.args[1])

    def test_sync_failure_rolls_back_cursor_and_partial_rows(self):
        with closing(open_index(":memory:")) as connection:
            client = Mock()
            client.request.side_effect = [[comment(), comment({**result(), "login": "wrong"})], issue()]
            with self.assertRaises(VerificationError):
                sync_index(connection, client)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT value FROM metadata WHERE key = 'last_sync'").fetchone())

    def test_old_bio_migration_reads_existing_contributor_without_editing_release(self):
        with closing(open_index(":memory:")) as connection:
            profile = result()
            profile.pop("bioFormatVersion")
            store_result(connection, profile, request())
            with patch("scripts.verify_profile.observe_profile", return_value={"status": "verified", "bio": "first\nsecond", "message": "Reobserved."}) as observe:
                refresh_bios(connection)
            observe.assert_called_once_with("rD227", source_url=RELEASE_URL)
            profile = json.loads(connection.execute("SELECT result FROM profiles").fetchone()[0])
            self.assertEqual(profile["bioFormatVersion"], 2)
            self.assertEqual(profile["bio"], "first\nsecond")

    def test_new_release_source_survives_indexing_and_export(self):
        with closing(open_index(":memory:")) as connection, TemporaryDirectory() as directory:
            profile = {**result(), "releaseUrl": RELEASE_URL.removesuffix("v0.1.1") + "v0.1.2"}
            store_result(connection, profile, request())
            export_index(connection, Path(directory))
            saved = json.loads((Path(directory) / "profiles" / "users" / "rd227.json").read_text())
            self.assertEqual(saved["releaseUrl"], profile["releaseUrl"])

    def test_sync_non404_removes_existing_record_and_never_admits_visible_user(self):
        for status in (200, 301, 302):
            with self.subTest(status=status), closing(open_index(":memory:")) as connection:
                with connection:
                    store_result(connection, result(), request())
                self.page_status.return_value = status
                client = Mock()
                client.request.side_effect = [[comment()], issue()]
                sync_index(connection, client)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)

    def test_sync_old_result_requires_live_404_before_admission(self):
        with closing(open_index(":memory:")) as connection:
            old = result()
            old.pop("profilePageStatus")
            client = Mock()
            client.request.side_effect = [[comment(old)], issue()]
            sync_index(connection, client)
            self.page_status.assert_called_once_with("rD227")
            stored = json.loads(connection.execute("SELECT result FROM profiles").fetchone()[0])
            self.assertEqual(stored["profilePageStatus"], 404)

    def test_excluded_event_removes_record_and_invalid_evidence_rolls_back(self):
        for status in (200, 301, 403, 404, 429, 500, None):
            with self.subTest(status=status), closing(open_index(":memory:")) as connection:
                with connection:
                    store_result(connection, result(), request())
                excluded = {**result(), "status": "excluded", "profilePageStatus": status, "login": "RD227"}
                client = Mock()
                client.request.side_effect = [[comment(excluded)], issue()]
                if status in (200, 301):
                    sync_index(connection, client)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)
                else:
                    with self.assertRaises(VerificationError):
                        sync_index(connection, client)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 1)
                    self.assertIsNone(connection.execute("SELECT value FROM metadata WHERE key = 'last_sync'").fetchone())
                self.page_status.assert_not_called()

    def test_unknown_page_status_preserves_database_and_sync_cursor(self):
        with closing(open_index(":memory:")) as connection:
            with connection:
                store_result(connection, result(), request())
                connection.execute("INSERT INTO metadata VALUES ('last_sync', '2026-11-19T01:00:00Z')")
            self.page_status.side_effect = VerificationError("Visibility unknown.")
            client = Mock()
            client.request.side_effect = [[comment()], issue()]
            with self.assertRaises(VerificationError):
                sync_index(connection, client)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key = 'last_sync'").fetchone()[0], "2026-11-19T01:00:00Z")

    def test_export_removes_stale_user_files_and_extra_pages_after_exclusion(self):
        with closing(open_index(":memory:")) as connection, TemporaryDirectory() as directory:
            for index in range(7):
                login = f"user{index}"
                store_result(connection, result(login), request(login))
            root = Path(directory) / "profiles"
            export_index(connection, Path(directory))
            self.assertTrue((root / "pages" / "2.json").exists())
            connection.execute("DELETE FROM profiles WHERE login != 'user0'")
            export_index(connection, Path(directory))
            self.assertEqual({path.name for path in (root / "users").glob("*.json")}, {"user0.json"})
            self.assertEqual({path.name for path in (root / "pages").glob("*.json")}, {"1.json"})
            connection.execute("DELETE FROM profiles")
            export_index(connection, Path(directory))
            self.assertEqual(list((root / "users").glob("*.json")), [])
            self.assertEqual(json.loads((root / "pages" / "1.json").read_text()), [])
            self.assertEqual(json.loads((root / "index.json").read_text())["total"], 0)

    def test_bio_migration_removes_non404_account_without_reading_hovercard(self):
        with closing(open_index(":memory:")) as connection:
            old = result()
            old.pop("bioFormatVersion")
            store_result(connection, old, request())
            self.page_status.return_value = 200
            with patch("scripts.verify_profile.observe_profile") as observe:
                refresh_bios(connection)
            observe.assert_not_called()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
