# BSL controller protocol (as used for Omni Xe autofocus)

Notes on the BSL controller protocol as the Omni Xe uses it (matching ComMarker Studio v1.0.17).
Status: **verified on hardware** (Omni Xe 6W, 2026-10-01) for everything this tool uses: framing,
state queries, the sensor pass-through, Z moves and the flash reads. Sections note what was
confirmed; the log is in [hardware-testing.md](hardware-testing.md).

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
Bit 0 set = axis moving. `reply[12..15]` / `reply[16..19]` (BE u32) = position counters of axes
0 / 1, origin `0x40000000`. Hardware-confirmed for axis 1: a +800 pulse move changed it by +800.

### `0xAAC1` RS-485 pass-through (`Executor7::setDataTransmit2`)
Data = bytes to transmit (≤ 251). Reply: `reply[2]` = number of bytes received, data at `reply[4]`.
An empty `AAC1` just collects received bytes.

Height read sequence (ComMarker): `AAC1 + 01 04 00 00 00 02 71 CB` (Modbus RTU, slave 1, read input
registers 0–1), wait 200 ms, `AAC1` with no data → Modbus reply
`01 04 04 b0 b1 b2 b3 crc crc`; height mm = BE u32(b0..b3) / 1000. A value of `7F FF FF FF`
means the sensor has no valid measurement (observed on hardware when no surface was in range).
Hardware-confirmed: the first AAC1 reply carries 0 received bytes; the collect reply carries the
9-byte Modbus frame.

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

### `0xAA10` run state (immediate)
12-byte form with a BE u16 parameter at offset 2: `AA 10 00 03` = run (`Executor7::setRun`),
`AA 10 00 01` = reset/stop (`setReset`). **List commands are only executed in the run state.**
Hardware-confirmed: a list sent without it is acknowledged but held: the status flag below drops to 0
and nothing moves.

### `0x0A00` delay (list command)
`sendDelayTime(n)`: 4-byte BE value `2n`. `0A 00 08 00 00 00 00 02` for n = 1.

### `0x0242` relative galvo move (list command)
`sendMoveToRel((0,0), 0, 1)` = `02 42 12 00 | 00 00 | 00 01 | 00 00 00 | 00 00 00 | 00 00 00 00`.
Used by `Executor::moveAxis`, but not by the path ComMarker uses for Z.

### Status flag
`AA05` `reply[12]` bit 0 (`getBit(0x0A, 0)`): `Executor::waitForFinish` stops waiting when it is set
or the board state is <= 1. Hardware: 1 when idle with nothing queued, 0 while a list is held.

### List transmission
List commands are concatenated (padded to >= 12 bytes with a zero filler command whose length byte
holds the pad size) and sent on the data port. Optional LZMA compression (type 2, header
`EF EF 00 02`) exists but uncompressed is the vendor's fallback, so it is not used.

### Z move sequence (`MarkControl::doMoveAxisPulse`, used by autofocus and the Z dialog)
1. `AA10` reset, sleep 10 ms, `AA10` run
2. list `[0x03A0 axis move, 0x0A00 delay(1)]` on the data port (after the free-cache check)
3. sleep 5 ms, poll `AA05` until finished (flag set or state <= 1)
4. `AA10` reset

ComMarker's autofocus thread then sleeps 2 s. This tool additionally waits at least the estimated
travel time and for the `AA07` moving bit to clear before the final reset.

## 5. Factory calibration in the controller's flash

The controller's flash holds a small file store with ComMarker Studio's parameter files
(`lcsparam.cfg`, `lcsparam_red.cfg`, `uiparam.cfg`, lens correction files). ComMarker's
`LoadParamFromFlash` downloads them when it first meets a machine, so the per-machine focus
heights (`fBestFocalDistance`, `fBestFocalDistance_B`) come from the laser, not from user input.

Commands (all 12-byte form, read-only):

| command | purpose | reply |
|---|---|---|
| `AAE0` | flash user area | `reply[4..7]` start, `reply[8..11]` end address (BE) |
| `AAE1` | flash state | `reply[4]` bit0 busy, bit1 read buffer ready, bit2 busy |
| `AAE4` | load into buffer | request: `AA E4 01 00`, BE length (≤ 480, even), BE address |
| `AAE5` | fetch buffer | `reply[4..5]` length (BE), data from `reply[6]` |

Read loop (`Executor7::readFlashData`): wait until idle (bit0 and bit2 clear), then for each
chunk of up to 480 bytes, `AAE4`, wait for bit1 set and bit0 clear, then `AAE5`.

Store layout (little-endian), at offsets within the user area: 4 KiB index at 0 (backup at
0x1000). The index CRC16 (`u16 @0`) covers bytes 2..4095 and equals the Modbus CRC16 with its
bytes swapped. The header is `u16 0x1000 @2, u32 1 @4, u32 0x1000 @8` (sector size). There are 50
entries of 0x50 bytes from 0x60: `u16 crc16 @0` (of bytes 2..0x4F), `u16 sector @2`,
`u32 stored size @4`, `u64 CRC-64/ECMA-182 of the stored bytes @0x10`, `u8 flags @0x18`
(bit0 = compressed) and a NUL-terminated name `@0x1C` such as `./config/lcsparam.cfg`. Data
starts at `sector × 0x1000`. Compressed files use the classic `.lzma` layout: 5 property bytes,
an 8-byte LE uncompressed size, then the raw LZMA stream.

On top of that, ComMarker Studio stores its files `qCompress`-ed (4-byte BE length, then a zlib
stream) and un-compresses them in `LoadParamFromFlash`. The result is the usual obfuscated `.cfg`
(section 2).

Hardware (Omni Xe 6W, 2026-10-01): user area `0x0..0x7FFFFF`. The store holds `lcsparam.cfg`,
`lcsparam_red.cfg`, `uiparam.cfg` and `threedparam.cfg` at sectors 2/66/130/194, store flags 0
(no LZMA) and all `qCompress`-ed, with CRC64s matching. The factory `lcsparam.cfg` holds focus
181/222 mm and fields 70/150 mm, and selects lens A (`IsGALVO_B` false).

Timing: sleep about 1 ms before each `AAE1` poll, as the vendor does. Polling immediately after
`AAE4` can see the previous transfer's "buffer ready" bit, and `AAE5` then returns 0 bytes.

