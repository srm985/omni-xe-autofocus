"""Bundle the LightBurn integration kit into dist/omni-autofocus-lightburn-kit-<version>.zip.

The zip is self-contained (kit, protocol notes, hardware log, reference source, licence), so it can be
handed over while the repository is still private.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from omni_autofocus import __version__

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    "LICENSE",
    "README.md",
    "docs/protocol.md",
    "docs/hardware-testing.md",
    *sorted(str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "docs" / "lightburn-kit").iterdir()),
    *sorted(
        str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "src" / "omni_autofocus").glob("*.py")
    ),
]

START_HERE = """Omni X / Xe autofocus: integration kit for LightBurn

Start with docs/lightburn-kit/README.md, then docs/lightburn-kit/integration-guide.md.
Byte-exact test vectors: docs/lightburn-kit/test-vectors.json.
Protocol notes: docs/protocol.md. Hardware log: docs/hardware-testing.md.
Reference implementation (Python, standard library only): src/omni_autofocus/.

By Sean (github.com/srm985). MIT licence (LICENSE): free to use; please credit.
"""


def main() -> None:
    out = ROOT / "dist" / f"omni-autofocus-lightburn-kit-{__version__}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("START-HERE.txt", START_HERE)
        for name in FILES:
            z.write(ROOT / name, name)
    print(f"wrote {out} ({len(FILES) + 1} files)")


if __name__ == "__main__":
    main()
