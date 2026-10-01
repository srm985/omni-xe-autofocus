# BSL controller protocol (as used for Omni Xe autofocus)

Notes on the BSL controller protocol as the Omni Xe uses it (matching ComMarker Studio v1.0.17).
Status: **implemented and unit-tested against reference
vectors; not yet verified on hardware** — see [hardware-testing.md](hardware-testing.md).

## 1. How ComMarker Studio autofocuses

`MarkSet::UiGWK_CalcMovePluses` + `AdjustHeightThread`:

1. Read the height sensor (up to 10 attempts).
2. Reject the reading if it is outside `[fMinDistanceOfSensor, fMaxDistanceOfSensor]` (120–280 mm).
3. `target = IsGALVO_B ? fBestFocalDistance_B : fBestFocalDistance` (222 / 181 mm on this machine).
4. `move = target − height`; do nothing if `|move| < 0.1 mm`.
5. `pulses = pitchPulse × move / screwPitch` using the `axisZParExt` axis parameters
   (axis 1, 3200 pulses/rev, 4 mm/rev → 800 pulses/mm, `bRevRot = true`).
6. `doMoveAxisPulse(axisZParExt, pulses)`, then sleep 2 s.

All values come from `config/lcsparam.cfg`.

## 2. Config file obfuscation

`config/*.cfg` = 8-byte header (`01 00 00 00 00 00 00 00`) + UTF-8 JSON XOR-ed with the repeating
16-byte key `"this is elfive\0\0"`. See `config.decode_commarker_cfg`.

## 3. USB transport

* Device: Cypress FX2LP, VID `04B4` PID `1004` (also accepted: `7580:0101`), driver **CYUSB3**,
  interface GUID `{AE18AA60-7F6A-11D4-97DD-00010229B959}`.
* Access is via CyAPI semantics: `DeviceIoControl(IOCTL 0x0022004B /*SEND_NON_EP0_DIRECT*/,
  in = 38-byte SINGLE_TRANSFER (endpoint address at offset 0x0D, rest zero), out = data buffer)`,
  overlapped. On timeout: `IOCTL 0x00220044` (abort pipe, 1-byte endpoint argument).
* Endpoints: command port OUT `0x06` / IN `0x88`; data (list) port OUT `0x02` / IN `0x84`.
* Timeouts: 100 ms per write and per read wait.

### Board protocol detection

The vendor sends an **unframed** 12-byte command `01 23 00…`. A reply starting `01 23` means the old
protocol 0 (`Executor5`, unframed). Anything else → protocol 1 (`Executor7`), which enables framing
with padding. The RS-485 pass-through only exists in `Executor7`, so the Omni Xe is protocol 1 and
this tool always uses framing.

### Frame (`TransferPackage`)

| offset | meaning |
|---|---|
| 0–1 | `FE FF` |
| 2–3 | total frame length N, big-endian |
| 4 | `bit7` command port, `bits4-6` retry count, `bits0-3` sequence |
| 5 | device→host status; `bits1-3` non-zero = transport error |
| 6… | payload, zero-padded to an even length ≥ 12 |
| N−2 | `00` |
| N−1 | checksum = (b[6] + b[7] + b[N−4] + b[N−3]) mod 256 |

Command port: sequence is always 0 over USB (the counter is never advanced); one resend on timeout
(retry bits = 1), up to 3 reads per send; the reply payload must echo the 2-byte command code.

Data port: sequence cycles 1…15 per new list packet; up to 6 sends; the reply payload starts
`FA 01` (or `FA 02` when acknowledging a resend).

## 4. Commands (`LcsCmd`)

`code (BE u16) | total length (u8) | flag (u8) | data…`; numeric fields are big-endian. State
queries use a 12-byte form (code + 10 zero bytes). In replies, "`getChar(i)`" = `reply[i + 2]`, and
`reply[2]` must be 0 for state queries.

### `0xAA05` device state
`reply[4]` = board state (≤ 1 idle). `reply[13..15]` = free list cache in KiB.

### `0xAA07` extended state
`reply[0x28]` low nibble = axis 0 status, high nibble = axis 1; `reply[0x29]` low nibble = axis 2.
Bit 0 set = axis moving.

### `0xAAC1` RS-485 pass-through (`Executor7::setDataTransmit2`)
Data = bytes to transmit (≤ 251). Reply: `reply[2]` = number of bytes received, data at `reply[4]`.
An empty `AAC1` just collects received bytes.

Height read sequence (ComMarker): `AAC1 + 01 04 00 00 00 02 71 CB` (Modbus RTU, slave 1, read input
registers 0–1), wait 200 ms, `AAC1` with no data → Modbus reply
`01 04 04 b0 b1 b2 b3 crc crc`; height mm = BE u32(b0..b3) / 1000.

### `0x03A0` / `0x03A1` axis move (list command, axes 0/1 or 2/3)
24 data bytes, two slots (slot = axis id mod 2):

| data offset | field |
|---|---|
| 0 | bit0/bit1 = direction of slot 0/1, bit2/bit3 = homing mode of slot 0/1 |
| 1 | bit0/bit1 = `signalValidType == 1` for slot 0/1 |
| 2–5, 6–9 | pulse count slot 0, slot 1 (u32) |
| 10–11, 12–13, 14 | slot 0: start freq, run freq (pulses/s, ≤ 65535), accel byte |
| 15–16, 17–18, 19 | slot 1: same |
| 20–21, 22–23 | accel distance (pulses, ≤ 65535) slot 0, slot 1 |

With `ppm = pitchPulse / screwPitch` (float32), speeds in mm/s: `run = max(run, start)`, capped at
`maxRunSpeed`; `t = (run − start) / acc`; accel byte = `0xFF` if `t > 0.255` else `int(t × 1000)`;
accel distance = `(startSpeed + runSpeed) × t × 0.5 × ppm`. Count = |pulses| × gearRatio (≥ 1),
direction bit = `(pulses < 0) XOR bRevRot`. The unused slot carries default `ExtAxisPar` values
(10000 pulses/rev, 10 mm, 2/10 mm/s, acc 10).

### `0x0242` relative galvo move
Appended after every axis move by the vendor (`sendMoveToRel((0,0), 0, 1)`):
`02 42 12 00 | 00 00 | 00 01 | 00 00 00 | 00 00 00 | 00 00 00 00`.

### List transmission
List commands are concatenated (padded to ≥ 12 bytes with a zero filler command whose length byte
holds the pad size) and sent on the data port. Optional LZMA compression (type 2, header
`EF EF 00 02`) exists but uncompressed is the vendor's fallback, so it is not used.

Move sequence (`Executor::moveAxis(..., wait=true)`): wait for cache (`AA05`), send
`[0x03A0, 0x0242]`, poll `AA05` until idle, poll `AA07` until the axis' moving bit clears.
