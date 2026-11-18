import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.build_pages import ReleaseAvatarParser, build


AVATAR = "https://avatars.githubusercontent.com/u/170849438?s=40&v=4"
SOURCE = "https://github.com/xusu616/android_kernel_xiaomi_markw-4.9/releases/tag/v1.1.0-ksun-susfs"


class ReleaseAvatarTests(unittest.TestCase):
    def test_only_uses_matching_release_byline(self):
        html = f'''
            <img alt="@rD227" class="avatar" src="https://avatars.githubusercontent.com/u/123?v=4">
            <div class="tmp-mr-4 mb-2">
              <img alt="@rD227" class="avatar avatar-small" src="{AVATAR}">
              <a href="/rD227" data-hovercard-type="user">rD227</a> tagged this
              <relative-time datetime="2026-11-18T07:59:48Z"></relative-time>
            </div>'''
        selected = ReleaseAvatarParser("RD227")
        selected.feed(html)
        self.assertEqual(selected.avatars, {AVATAR})

        wrong_login = ReleaseAvatarParser("xusu616")
        wrong_login.feed(html)
        self.assertEqual(wrong_login.avatars, set())

    def test_builds_cards_without_an_avatar_field(self):
        card = {
            "login": "rD227", "name": "rD227", "bio": "", "badge": "",
            "releaseUrl": SOURCE, "observedAt": "2026-11-18",
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cards").mkdir()
            (root / "site").mkdir()
            for filename in ("index.html", "styles.css", "app.js"):
                (root / "site" / filename).touch()
            (root / "cards" / "record.json").write_text(json.dumps(card), encoding="utf-8")
            with patch("scripts.build_pages.release_avatar", return_value=AVATAR) as lookup:
                self.assertEqual(build(root, root / "dist"), 1)
            lookup.assert_called_once_with(SOURCE, "rD227")
            records = json.loads((root / "dist" / "cards.json").read_text(encoding="utf-8"))
            self.assertEqual(records[0]["avatarUrl"], AVATAR)
