import pytest

from omni_autofocus import commands, modbus
from omni_autofocus.commands import AxisParams

Z_AXIS = AxisParams(
    axis_id=1, reverse=True, pitch_pulse=3200, screw_pitch=4.0, start_speed=0.0, run_speed=8.0, acc_speed=5.0
)


def test_modbus_height_request_matches_vendor_bytes():
    assert commands.HEIGHT_REQUEST == bytes.fromhex("01 04 00 00 00 02 71 cb")


def test_modbus_crc_known_vector():
    assert modbus.crc16(bytes.fromhex("01 04 00 00 00 02")) == 0xCB71


def test_parse_height_reply():
    reply = modbus.with_crc(bytes.fromhex("01 04 04 00 03 63 30"))  # 222000 um
    assert commands.parse_height_reply(reply) == pytest.approx(222.0)


def test_parse_height_reply_no_target_hardware_vector():
    # Captured from the real sensor (2026-10-01) with nothing in range; CRC D3 D0 is the sensor's own.
    with pytest.raises(commands.SensorNoTarget):
        commands.parse_height_reply(bytes.fromhex("01 04 04 7f ff ff ff d3 d0"))


def test_parse_height_reply_rejects_bad_crc():
    reply = bytearray(modbus.with_crc(bytes.fromhex("01 04 04 00 03 63 30")))
    reply[-1] ^= 1
    with pytest.raises(modbus.ModbusError):
        commands.parse_height_reply(bytes(reply))


def test_parse_height_reply_rejects_exception_response():
    with pytest.raises(modbus.ModbusError):
        commands.parse_height_reply(modbus.with_crc(bytes.fromhex("01 84 02")))


def test_data_transmit_encoding():
    assert commands.data_transmit(commands.HEIGHT_REQUEST) == bytes.fromhex(
        "aa c1 0c 00 01 04 00 00 00 02 71 cb"
    )
    assert commands.data_transmit(b"") == bytes.fromhex("aa c1 04 00")


def test_data_transmit_reply_parsing():
    assert commands.parse_data_transmit_reply(bytes.fromhex("aa c1 03 00 01 02 03 ff ff")) == b"\x01\x02\x03"
    with pytest.raises(ValueError):
        commands.parse_data_transmit_reply(bytes.fromhex("aa c1 09 00 01"))


def test_status_cmd_is_fixed_12_bytes():
    assert commands.status_cmd(0xAA05) == bytes.fromhex("aa 05") + bytes(10)


def test_z_axis_move_golden():
    # The axis-move command for the Omni's Z axis settings (axisZParExt).
    # Z is slot 1; slot 0 carries vendor-default ExtAxisPar values (1000 pulses/mm, 2/10 mm/s, acc 10).
    expected = bytes.fromhex(
        "03 a0 1c 00"
        "02 00"  # flags: slot1 direction bit (positive move XOR reverse), signal bits
        "00 00 00 00"  # slot0 count
        "00 00 03 20"  # slot1 count = 800
        "07 d0 27 10 ff"  # slot0 start 2000 Hz, run 10000 Hz, accel byte (t=0.8 s -> 0xff)
        "00 00 19 00 ff"  # slot1 start 0 Hz, run 6400 Hz, accel byte (t=1.6 s -> 0xff)
        "12 c0"  # slot0 accel distance 4800 pulses
        "14 00"  # slot1 accel distance 5120 pulses
    )
    assert commands.axis_move_cmd(Z_AXIS, 800) == expected


def test_negative_move_flips_direction_only():
    pos = commands.axis_move_cmd(Z_AXIS, 800)
    neg = commands.axis_move_cmd(Z_AXIS, -800)
    assert neg[4] == 0x00 and pos[4] == 0x02
    assert neg[5:] == pos[5:]


def test_axis_move_short_ramp_accel_byte():
    fast = AxisParams(axis_id=0, start_speed=2.0, run_speed=4.0, acc_speed=100.0)  # t = 0.02 s
    cmd = commands.axis_move_cmd(fast, 10)
    assert cmd[4 + 14] == 20  # milliseconds


def test_axis_2_and_3_use_second_command():
    cmd = commands.axis_move_cmd(AxisParams(axis_id=3), 5)
    assert cmd[:2] == b"\x03\xa1"
    assert cmd[4 + 6 : 4 + 10] == (5).to_bytes(4, "big")


def test_move_to_rel_default():
    assert commands.move_to_rel_cmd() == bytes.fromhex("02 42 12 00 00 00 00 01 00 00 00 00 00 00") + bytes(4)


def test_command_list_padding():
    assert commands.command_list(b"\x00\x01\x04\x00") == bytes.fromhex("00 01 04 00 00 00 08 00") + bytes(4)
    long = commands.command_list(commands.axis_move_cmd(Z_AXIS, 1), commands.move_to_rel_cmd())
    assert len(long) == 28 + 18


def test_mm_to_pulses():
    assert Z_AXIS.pulses_per_mm == 800.0
    assert Z_AXIS.mm_to_pulses(1.0) == 800
    assert Z_AXIS.mm_to_pulses(-2.5) == -2000


def test_dev_state_parsing():
    reply = bytearray(0x40)
    reply[0:2] = b"\xaa\x05"
    reply[4] = 1
    reply[13:16] = b"\x00\x04\x00"
    st = commands.parse_dev_state(bytes(reply))
    assert st.board_state == 1 and st.idle and st.free_cache_kb == 1024
    reply[2] = 1
    with pytest.raises(ValueError):
        commands.parse_dev_state(bytes(reply))


def test_ext_state_axis_nibbles():
    reply = bytearray(0x40)
    reply[0:2] = b"\xaa\x07"
    reply[0x28] = 0x10
    reply[0x29] = 0x01
    ext = commands.parse_dev_ext_state(bytes(reply))
    assert not ext.axis_moving(0) and ext.axis_moving(1) and ext.axis_moving(2)
