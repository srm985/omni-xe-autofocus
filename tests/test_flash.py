import pytest

from omni_autofocus import config, flash
from omni_autofocus.controller import Controller
from omni_autofocus.simulator import FakeClock, SimulatedBoard


def make(files, **kw):
    board = SimulatedBoard(flash_image=flash.build_store(files, **kw))
    clock = FakeClock()
    return board, Controller(board, sleep=clock.sleep, clock=clock)


def test_crc64_ecma182_check_value():
    assert flash.crc64(b"123456789") == 0x6C40DF5F0B497347


def test_crc16_is_byte_swapped_modbus():
    # Reference value for this byte-swapped Modbus CRC.
    assert flash.crc16(bytes.fromhex("01 04 00 00 00 02")) == 0x71CB


def test_lzma_roundtrip_uses_props_size_header():
    data = b"hello flash " * 100
    packed = flash.compress(data)
    assert len(packed) > 13 and int.from_bytes(packed[5:13], "little") == len(data)
    assert flash.decompress(packed) == data


def test_index_roundtrip():
    image = flash.build_store({"./config/a.cfg": b"x" * 10, "./config/b.cfg": b"y" * 5000})
    entries = flash.parse_index(image[: flash.INDEX_SIZE])
    assert [e.name for e in entries] == ["./config/a.cfg", "./config/b.cfg"]
    assert entries[0].sector == 2 and entries[1].sector == 3 and all(e.compressed for e in entries)


@pytest.mark.parametrize("size", [1, 479, 480, 481, 4095, 9001])
def test_read_named_across_chunk_boundaries(size):
    content = bytes((i * 7) & 0xFF for i in range(size))
    board, ctl = make({"./config/blob.bin": content}, compressed=False)
    assert flash.read_named(ctl, "blob.bin") == content


def test_read_compressed_commarker_config_end_to_end():
    cfg = {
        "lmcPars": {
            "params": [
                {
                    "parName": "default",
                    "IsGALVO_B": True,
                    "fBestFocalDistance": 180.0,
                    "fBestFocalDistance_B": 223.5,
                    "galvoParam": {"workSize": {"x": 70.0, "y": 70.0}},
                    "galvo2Param": {"workSize": {"x": 150.0, "y": 150.0}},
                }
            ]
        }
    }
    raw = config.encode_commarker_cfg(cfg)
    board, ctl = make({"./config/uiparam.cfg": b"{}", "./config/lcsparam.cfg": raw})
    got = flash.read_named(ctl, "lcsparam.cfg")
    assert config.decode_commarker_cfg(got) == cfg


def test_corrupt_data_is_detected():
    image = bytearray(flash.build_store({"./config/x.cfg": b"payload" * 50}))
    image[2 * flash.INDEX_SIZE + 20] ^= 0xFF
    board = SimulatedBoard(flash_image=bytes(image))
    ctl = Controller(board, sleep=FakeClock().sleep)
    with pytest.raises(flash.FlashError, match="CRC64"):
        flash.read_named(ctl, "x.cfg")


def test_backup_index_is_used_when_primary_is_corrupt():
    image = bytearray(flash.build_store({"./config/x.cfg": b"payload"}))
    image[100] ^= 0xFF  # damage the primary index only
    clock = FakeClock()
    ctl = Controller(SimulatedBoard(flash_image=bytes(image)), sleep=clock.sleep, clock=clock)
    assert flash.read_named(ctl, "x.cfg") == b"payload"


def test_missing_file_is_reported():
    board, ctl = make({"./config/x.cfg": b"1"})
    with pytest.raises(flash.FlashError, match="not stored"):
        flash.read_named(ctl, "lcsparam.cfg")


def test_flash_load_command_encoding():
    board, ctl = make({"./config/x.cfg": b"abc"}, compressed=False)
    flash.read_named(ctl, "x.cfg")
    from omni_autofocus import framing

    loads = [framing.parse_frame(f).payload[:12] for ep, f in board.sent_frames if f[6:8] == b"\xaa\xe4"]
    # index read: first chunk of 480 bytes at the user-area start
    assert loads[0] == bytes.fromhex("aa e4 01 00 00 00 01 e0") + board.flash_start.to_bytes(4, "big")


def test_cli_first_run_saves_calibration_from_the_laser(capsys, tmp_path):
    from omni_autofocus import config
    from omni_autofocus.cli import main

    cfg = tmp_path / "first.toml"
    assert main(["--simulate", "--config", str(cfg), "--lens", "b", "focus", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "First run: saved the factory calibration from the laser" in out
    saved = config.load(cfg)
    assert (saved.focus.target_a_mm, saved.focus.target_b_mm, saved.focus.lens) == (181.0, 222.0, "auto")


def test_cli_calibration_command_compares_and_saves(capsys, tmp_path):
    from omni_autofocus import config
    from omni_autofocus.cli import main

    cfg = tmp_path / "cal.toml"
    s = config.Settings()
    config.save(type(s)(focus=type(s.focus)(target_b_mm=225.0, offset_mm=0.3)), cfg)
    assert main(["--simulate", "--config", str(cfg), "calibration"]) == 0
    out = capsys.readouterr().out
    assert "lens B focus (sensor mm)" in out and "<- differs" in out
    assert main(["--simulate", "--config", str(cfg), "calibration", "--save"]) == 0
    saved = config.load(cfg)
    assert saved.focus.target_b_mm == 222.0 and saved.focus.offset_mm == 0.3  # offset kept


def test_cli_without_any_calibration_source_refuses(monkeypatch, capsys, tmp_path):
    from omni_autofocus import cli, session

    def no_laser(self):
        raise cli.ControllerError("no device")

    monkeypatch.setattr(session.Session, "open", no_laser)
    code = cli.main(["--force", "--config", str(tmp_path / "none.toml"), "--lens", "b", "focus", "--dry-run"])
    assert code == 1
    assert "no device" in capsys.readouterr().err
    assert not (tmp_path / "none.toml").exists()  # no fallback values saved without a laser


def test_calibration_falls_back_to_commarker_when_the_store_is_unreadable(monkeypatch, tmp_path):
    from omni_autofocus import config, session

    monkeypatch.setattr(config, "COMMARKER_DIR", tmp_path / "cm")
    (tmp_path / "cm" / "config").mkdir(parents=True)
    cfg = {
        "lmcPars": {
            "params": [{"parName": "default", "fBestFocalDistance": 180.0, "fBestFocalDistance_B": 219.5}]
        }
    }
    (tmp_path / "cm" / "config" / "lcsparam.cfg").write_bytes(config.encode_commarker_cfg(cfg))

    def unreadable(ctl):
        raise flash.FlashError("no store")

    monkeypatch.setattr(session, "read_laser_calibration", unreadable)
    s, source = session.Session(config_path=tmp_path / "x.toml", simulate=True).factory_calibration()
    assert source == "ComMarker Studio's settings" and s.focus.target_b_mm == 219.5

    monkeypatch.setattr(config, "COMMARKER_DIR", tmp_path / "missing")
    with pytest.raises(session.ControllerError, match="no focus calibration found"):
        session.Session(config_path=tmp_path / "x.toml", simulate=True).factory_calibration()


def test_qt_compress_roundtrip_and_hardware_header_shape():
    data = b"\x01" + bytes(7) + b"obfuscated json" * 500
    packed = flash.qt_compress(data)
    # Same shape as the controller's files (2026-10-01): BE length, then a zlib header 78 xx.
    assert packed[:4] == len(data).to_bytes(4, "big") and packed[4] == 0x78
    assert flash.qt_uncompress(packed) == data
    with pytest.raises(flash.FlashError, match="declared"):
        flash.qt_uncompress((len(data) + 1).to_bytes(4, "big") + packed[4:])


def test_read_commarker_file_handles_qcompressed_and_plain():
    raw = b"\x01" + bytes(7) + b"{}"
    board, ctl = make({"./config/q.cfg": flash.qt_compress(raw), "./config/p.cfg": raw}, compressed=False)
    assert flash.read_commarker_file(ctl, "q.cfg") == raw
    assert flash.read_commarker_file(ctl, "p.cfg") == raw


def test_calibration_without_focus_heights_is_rejected():
    from omni_autofocus import config

    with pytest.raises(ValueError, match="fBestFocalDistance_B"):
        config.from_commarker({"lmcPars": {"params": [{"parName": "default", "fBestFocalDistance": 181.0}]}})


def test_busy_laser_does_not_fall_back_to_commarker(monkeypatch, tmp_path):
    from omni_autofocus import session
    from omni_autofocus.controller import ControllerBusyError

    def busy(ctl):
        raise ControllerBusyError("busy")

    monkeypatch.setattr(session, "read_laser_calibration", busy)
    with pytest.raises(ControllerBusyError):
        session.Session(config_path=tmp_path / "x.toml", simulate=True).factory_calibration()
