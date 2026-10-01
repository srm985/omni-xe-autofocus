import pytest

from omni_autofocus import framing


def test_seq_byte_layout():
    assert framing.seq_byte(command_port=True, seq=0, retry=0) == 0x80
    assert framing.seq_byte(command_port=True, seq=0, retry=1) == 0x90
    assert framing.seq_byte(command_port=False, seq=5, retry=2) == 0x25


@pytest.mark.parametrize("n,expected", [(0, 12), (4, 12), (11, 12), (12, 12), (13, 14), (28, 28), (46, 46)])
def test_padding_rule(n, expected):
    assert framing.padded_length(n) == expected


def test_height_query_frame_golden():
    # AAC1 pass-through carrying the Modbus height request, as ComMarker Studio sends it.
    payload = bytes.fromhex("aa c1 0c 00 01 04 00 00 00 02 71 cb")
    frame = framing.build_frame(payload, seq=0x80)
    assert frame == bytes.fromhex("fe ff 00 14 80 00 aa c1 0c 00 01 04 00 00 00 02 71 cb 00") + bytes(
        [(0xAA + 0xC1 + 0x71 + 0xCB) & 0xFF]
    )


def test_short_payload_is_padded_and_checksummed():
    frame = framing.build_frame(bytes.fromhex("aa c1 04 00"), seq=0x80)
    assert len(frame) == 20
    assert frame[2:4] == b"\x00\x14"
    assert frame[6:10] == bytes.fromhex("aa c1 04 00")
    assert frame[10:18] == bytes(8)
    assert frame[-1] == (0xAA + 0xC1) & 0xFF


def test_parse_roundtrip():
    payload = bytes(range(30))
    parsed = framing.parse_frame(framing.build_frame(payload, seq=0x93))
    assert parsed.payload == payload
    assert parsed.sequence == 3 and parsed.retry == 1 and parsed.net_error == 0


def test_parse_accepts_trailing_bytes():
    frame = framing.build_frame(b"\x01\x02", seq=0)
    assert framing.parse_frame(frame + b"\xee\xee").payload[:2] == b"\x01\x02"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: b"\x00\x00" + f[2:],  # bad magic
        lambda f: f[:-1] + bytes([f[-1] ^ 0xFF]),  # bad checksum
        lambda f: f[:2] + b"\x01\x00" + f[4:],  # length beyond buffer
        lambda f: f[:5],  # truncated
    ],
)
def test_parse_rejects_corrupt_frames(mutate):
    frame = framing.build_frame(bytes(range(12)), seq=0)
    with pytest.raises(framing.FrameError):
        framing.parse_frame(mutate(frame))


def test_net_error_bits():
    frame = bytearray(framing.build_frame(bytes(12), seq=0))
    frame[5] = 0b0000_0110
    assert framing.parse_frame(bytes(frame)).net_error == 3
