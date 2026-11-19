import json
import re
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

    def test_published_html_references_matching_fingerprinted_assets(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cards").mkdir()
            (root / "site").mkdir()
            (root / "site" / "index.html").write_text(
                '<link href="./styles.css"><script src="./app.js" defer></script>', encoding="utf-8"
            )
            css, script = "body { color: green; }", "console.log('first');"
            (root / "site" / "styles.css").write_text(css, encoding="utf-8")
            (root / "site" / "app.js").write_text(script, encoding="utf-8")
            output = root / "dist"
            build(root, output)
            first_html = (output / "index.html").read_text(encoding="utf-8")
            first_assets = re.findall(r'\./([^" ]+)', first_html)
            self.assertEqual(len(first_assets), 2)
            for filename, content in zip(first_assets, (css, script)):
                self.assertRegex(filename, r"^(styles|app)\.[0-9a-f]{16}\.(css|js)$")
                self.assertEqual((output / filename).read_text(encoding="utf-8"), content)
            build(root, output)
            self.assertEqual((output / "index.html").read_text(encoding="utf-8"), first_html)
            (root / "site" / "app.js").write_text("console.log('second');", encoding="utf-8")
            build(root, output)
            second_assets = re.findall(r'\./([^" ]+)', (output / "index.html").read_text(encoding="utf-8"))
            self.assertEqual(second_assets[0], first_assets[0])
            self.assertNotEqual(second_assets[1], first_assets[1])
            self.assertEqual((output / second_assets[1]).read_text(encoding="utf-8"), "console.log('second');")
            self.assertEqual((output / first_assets[1]).read_text(encoding="utf-8"), script)
