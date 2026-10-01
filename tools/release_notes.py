"""Write the GitHub release notes for a tag: download instructions, then the CHANGELOG section.

Usage: python tools/release_notes.py v1.0.0 srm985/omni-xe-autofocus release-notes.md
Run after the build, so the installer's SHA-256 can be included.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "dist" / "OmniAutofocus-Setup.exe"

HEAD = """\
**Download [OmniAutofocus-Setup.exe]({download})** and run it. No administrator rights are needed.

Windows may say it "protected your PC" because the installer is not code-signed: click
**More info → Run anyway**. Needs Windows 10 or 11 (64-bit) and the laser's USB driver; see the
[README](https://github.com/{repo}#install).

Independent project, not affiliated with ComMarker, BSL or LightBurn.
"""


def changelog_section(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(version)} [^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    return m.group(1).strip()


def main() -> None:
    tag, repo, out = sys.argv[1:4]
    version = tag.removeprefix("v")
    download = f"https://github.com/{repo}/releases/download/{tag}/{INSTALLER.name}"
    parts = [HEAD.format(download=download, repo=repo)]
    if INSTALLER.exists():
        parts.append(f"SHA-256 of {INSTALLER.name}: `{hashlib.sha256(INSTALLER.read_bytes()).hexdigest()}`\n")
    parts.append(f"## What's in {version}\n\n{changelog_section(version)}\n")
    Path(out).write_text("\n".join(parts), encoding="utf-8")


if __name__ == "__main__":
    main()
