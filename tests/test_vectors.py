import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_lightburn_kit_vectors_are_current():
    spec = importlib.util.spec_from_file_location("export_vectors", ROOT / "tools" / "export_vectors.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    on_disk = (ROOT / "docs" / "lightburn-kit" / "test-vectors.json").read_text(encoding="utf-8")
    assert on_disk == mod.render(), "run: python tools/export_vectors.py"


def test_known_check_values():
    from omni_autofocus import flash, modbus

    assert modbus.crc16(b"123456789") == 0x4B37  # CRC-16/MODBUS check value
    assert flash.crc64(b"123456789") == 0x6C40DF5F0B497347  # CRC-64/ECMA-182 check value
