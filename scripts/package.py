"""Create downloadable source ZIP bundles for each desktop OS."""

import argparse
from pathlib import Path
import stat
import struct
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.0.0"
PACKAGES = {
    "windows": ("run_windows.bat",),
    "linux": ("run.sh",),
    "macos": ("run.sh",),
}
COMMON_FILES = (
    "hu_stream.py",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "assets/hu-screen-streamer.svg",
    "assets/hu-screen-streamer.png",
    "assets/hu-screen-streamer.ico",
    "native/hu-screen-mirror",
)


def create_package(platform):
    helper = ROOT / "native" / "hu-screen-mirror"
    if not helper.is_file():
        raise FileNotFoundError(
            "QNX helper is missing. Build it with native/build.ps1 or native/build.sh first."
        )
    header = helper.read_bytes()[:20]
    if (
        len(header) < 20
        or header[:4] != b"\x7fELF"
        or header[4] != 1
        or header[5] != 1
        or struct.unpack_from("<H", header, 18)[0] != 40
    ):
        raise ValueError("native/hu-screen-mirror must be a 32-bit little-endian ARM ELF helper.")

    files = COMMON_FILES + PACKAGES[platform]
    archive_path = ROOT / "dist" / f"nbt-evo-screen-utility-v{VERSION}-{platform}.zip"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    top_level = "nbt-evo-screen-utility"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative_path in files:
            source = ROOT / relative_path
            if not source.is_file():
                raise FileNotFoundError(f"Release input not found: {source}")
            archive_name = f"{top_level}/{relative_path}"
            info = zipfile.ZipInfo.from_file(source, archive_name)
            if relative_path == "run.sh":
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o755) << 16
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    print(f"Created {archive_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("all", *PACKAGES), default="all")
    args = parser.parse_args()
    platforms = PACKAGES if args.platform == "all" else (args.platform,)
    for platform in platforms:
        create_package(platform)


if __name__ == "__main__":
    main()
