from contextlib import closing
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts.profile_index import export_index, open_index, refresh_bios, store_result, sync_index
from scripts.verify_profile import API_ROOT, RELEASE_URL, REQUEST_MARKER, ProfileRequest, VerificationError, make_result, result_body, valid_request_id


REQUEST_ID = "95b7e76a-df4b-4d91-b507-bcf5bed45e44"


def request(login="rD227", number=42):
    return ProfileRequest(number, REQUEST_ID, login)


def result(login="rD227", observed_at="2026-11-19T01:00:00Z"):
    profile = make_result(request(login), {"status": "verified", "name": login, "bio": "first\nsecond", "badge": "", "avatarUrl": "", "message": "Observed.", "profilePageStatus": 404})
    profile["observedAt"] = observed_at
    return profile


def issue(login="rD227", number=42):
    return {"number": number, "title": f"Verify profile: {login}", "body": f"{REQUEST_MARKER}\nRequest ID: {REQUEST_ID}\nGitHub username: {login}\n"}


def comment(profile=None, number=42):
    return {"user": {"login": "github-actions[bot]", "type": "Bot"}, "issue_url": f"{API_ROOT}/issues/{number}", "body": result_body(profile or result())}


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
            index = json.loads((root / "index.json").read_text())
            self.assertTrue(valid_request_id(index["snapshotId"]))
            self.assertEqual(index, {"snapshotId": index["snapshotId"], "total": 13, "pageSize": 6, "pageCount": 3})
            self.assertEqual([len(json.loads((root / "pages" / f"{page}.json").read_text())) for page in (1, 2, 3)], [6, 6, 1])
            profile = json.loads((root / "users" / "user12.json").read_text())
            self.assertEqual(profile["bio"], "first\nsecond")
            self.assertEqual(profile["issueNumber"], 13)

    def test_each_export_labels_all_files_with_one_new_snapshot_id(self):
        with closing(open_index(":memory:")) as connection, TemporaryDirectory() as directory:
            for index in range(7):
                store_result(connection, result(f"user{index}"), request(f"user{index}", index + 1))
            records_before = connection.execute("SELECT result FROM profiles ORDER BY login").fetchall()
            root = Path(directory) / "profiles"
            snapshot_ids = []
            for _ in range(2):
                export_index(connection, Path(directory))
                snapshot_id = json.loads((root / "index.json").read_text())["snapshotId"]
                self.assertTrue(valid_request_id(snapshot_id))
                snapshot_ids.append(snapshot_id)
                users = [json.loads(path.read_text()) for path in (root / "users").glob("*.json")]
                pages = [json.loads(path.read_text()) for path in (root / "pages").glob("*.json")]
                self.assertEqual(len(users), 7)
                self.assertEqual(len(pages), 2)
                self.assertEqual({profile["snapshotId"] for profile in users}, {snapshot_id})
                self.assertEqual({profile["snapshotId"] for page in pages for profile in page}, {snapshot_id})
            self.assertNotEqual(snapshot_ids[0], snapshot_ids[1])
            self.assertEqual(connection.execute("SELECT result FROM profiles ORDER BY login").fetchall(), records_before)

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
            client.request.side_effect = [[comment(), comment({**result(), "badge": "invalid"})], issue()]
            with self.assertRaises(VerificationError):
                sync_index(connection, client)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT value FROM metadata WHERE key = 'last_sync'").fetchone())

    def test_sync_skips_edited_requests_without_blocking_valid_results(self):
        original = issue()
        changes = (
            {"title": "Done, thank you"},
            {"body": "The original request was removed."},
            {"body": original["body"].replace(REQUEST_ID, "851565cc-2fca-48c2-a0af-91f33f49ecdb")},
            {"body": original["body"].replace(REQUEST_ID, "12345678-1234-1234-1234-123456789abc")},
            {"title": "Verify profile: renamed-user"},
        )
        for change in changes:
            with self.subTest(change=change), closing(open_index(":memory:")) as connection:
                self.page_status.reset_mock()
                with connection:
                    store_result(connection, result(), request())
                    connection.execute("INSERT INTO metadata VALUES ('last_sync', '2026-11-19T01:00:00Z')")
                previous = connection.execute("SELECT result FROM profiles WHERE login = 'rD227'").fetchone()[0]
                client = Mock()
                client.request.side_effect = [
                    [comment(), comment(), comment(result("valid-user"), 43)],
                    {**original, **change}, issue("valid-user", 43),
                ]
                with patch("sys.stderr", new_callable=StringIO) as diagnostics:
                    sync_index(connection, client, datetime(2026, 11, 19, 2, tzinfo=timezone.utc))
                self.assertIn("issue #42", diagnostics.getvalue())
                self.assertEqual(client.request.call_count, 3)
                self.page_status.assert_called_once_with("valid-user")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT result FROM profiles WHERE login = 'rD227'").fetchone()[0], previous)
                self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key = 'last_sync'").fetchone()[0], "2026-11-19T01:59:59Z")

    def test_sync_skips_exclusion_for_a_changed_request_id(self):
        with closing(open_index(":memory:")) as connection:
            with connection:
                store_result(connection, result(), request())
            changed = issue()
            changed["body"] = changed["body"].replace(REQUEST_ID, "851565cc-2fca-48c2-a0af-91f33f49ecdb")
            excluded = {**result(), "status": "excluded", "profilePageStatus": 200}
            client = Mock()
            client.request.side_effect = [[comment(excluded)], changed]
            with patch("sys.stderr", new_callable=StringIO) as diagnostics:
                sync_index(connection, client)
            self.assertIn("no longer matches", diagnostics.getvalue())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 1)
            self.assertIsNotNone(connection.execute("SELECT value FROM metadata WHERE key = 'last_sync'").fetchone())
            self.page_status.assert_not_called()

    def test_issue_network_failure_still_rolls_back_partial_sync(self):
        with closing(open_index(":memory:")) as connection:
            client = Mock()
            client.request.side_effect = [
                [comment(), comment(result("valid-user"), 43)],
                issue(), VerificationError("GitHub REST API could not be reached."),
            ]
            with self.assertRaisesRegex(VerificationError, "could not be reached"):
                sync_index(connection, client)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT value FROM metadata WHERE key = 'last_sync'").fetchone())

    def test_cached_non_v4_ids_are_removed_once_without_resetting_sync_cursor(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "profiles.sqlite"
            with closing(open_index(database)) as connection, connection:
                store_result(connection, result(), request())
                legacy = {**result("legacy-user"), "requestId": "12345678-1234-1234-1234-123456789abc", "issueNumber": 43}
                connection.execute("INSERT INTO profiles VALUES (?, ?, ?, ?, ?)",
                                   ("legacy-user", legacy["observedAt"], 43, 2, json.dumps(legacy)))
                uppercase = {**result("uppercase-user"), "requestId": REQUEST_ID.upper(), "issueNumber": 44}
                connection.execute("INSERT INTO profiles VALUES (?, ?, ?, ?, ?)",
                                   ("uppercase-user", uppercase["observedAt"], 44, 2, json.dumps(uppercase)))
                connection.execute("INSERT INTO metadata VALUES ('last_sync', '2026-11-19T01:00:00Z')")
                connection.execute("DELETE FROM metadata WHERE key = 'request_id_format'")
            with patch("scripts.profile_index.valid_request_id", wraps=valid_request_id) as validate_id:
                with patch("sys.stderr", new_callable=StringIO) as diagnostics, closing(open_index(database)) as connection:
                    self.assertIn("@legacy-user", diagnostics.getvalue())
                    self.assertEqual(validate_id.call_count, 3)
                    self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key = 'last_sync'").fetchone()[0], "2026-11-19T01:00:00Z")
                    self.assertEqual(export_index(connection, Path(directory)), 2)
                    profiles = json.loads((Path(directory) / "profiles/pages/1.json").read_text())
                    self.assertEqual({profile["login"] for profile in profiles}, {"rD227", "uppercase-user"})
                with closing(open_index(database)) as connection:
                    self.assertEqual(validate_id.call_count, 3)
                    self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key = 'request_id_format'").fetchone()[0], "uuid-v4")

    def test_request_id_migration_does_not_hide_corrupted_database_records(self):
        with closing(open_index(":memory:")) as connection:
            with connection:
                connection.execute("INSERT INTO profiles VALUES ('broken', '2026-11-19T01:00:00Z', 42, 2, 'invalid json')")
                connection.execute("DELETE FROM metadata WHERE key = 'request_id_format'")
            with patch("scripts.profile_index.sqlite3.connect", return_value=connection):
                with self.assertRaises(json.JSONDecodeError):
                    open_index(":memory:")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 1)
            self.assertIsNone(connection.execute("SELECT value FROM metadata WHERE key = 'request_id_format'").fetchone())

    def test_store_result_rejects_non_v4_ids_even_when_request_matches(self):
        with closing(open_index(":memory:")) as connection:
            identifier = "12345678-1234-1234-1234-123456789abc"
            with self.assertRaises(VerificationError):
                store_result(connection, {**result(), "requestId": identifier}, ProfileRequest(42, identifier, "rD227"))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)

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
