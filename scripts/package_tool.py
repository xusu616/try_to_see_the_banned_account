import argparse
import hashlib
from pathlib import Path
from zipfile import ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "find-profile-cards.pyz"
ENTRY_POINT = b"from find_profile_cards import main\n\nraise SystemExit(main())\n"


def main():
    parser = argparse.ArgumentParser(description="打包可直接用 Python 运行的资料卡查找工具。")
    parser.add_argument("--output", type=Path, default=Path("dist"), help="输出目录")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = args.output / APP_NAME

    # Fixed member metadata and line endings keep rebuilds byte-for-byte identical.
    with ZipFile(app, "w") as archive:
        archive.writestr(ZipInfo("__main__.py"), ENTRY_POINT)
        for name in ("find_profile_cards.py", "LICENSE"):
            contents = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
            archive.writestr(ZipInfo(name), contents)

    digest = hashlib.sha256(app.read_bytes()).hexdigest()
    checksum = app.with_suffix(".pyz.sha256")
    checksum.write_bytes(f"{digest}  {app.name}\n".encode("ascii"))
    print(app)
    print(checksum)


if __name__ == "__main__":
    main()
