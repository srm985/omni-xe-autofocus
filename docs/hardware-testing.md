# Hardware bring-up checklist

Nothing in this tool has run against a real controller yet. Go through these steps in order and stop
at the first surprise. Run each command with `-vv` the first time so the raw USB frames are printed;
save the output if anything looks wrong.

Before every session: **close ComMarker Studio** and make sure **LightBurn is not connected to the
laser** (both poll the controller continuously). Keep a hand near the power switch for motion steps.

| # | Command | Moves hardware? | Expected |
|---|---|---|---|
| 1 | `omni-autofocus devices` | no | one `[supported]` line containing `vid_04b4&pid_1004` |
| 2 | `omni-autofocus -vv status --raw` | no | board state 0/1 (idle); replies start `fe ff` |
| 3 | `omni-autofocus -vv height` | no | a plausible distance (120–280 mm) |
| 4 | `omni-autofocus height -n 5` with a ruler: raise/lower an object under the sensor between readings | no | readings change by the object's height |
| 5 | `omni-autofocus focus --dry-run` | no | planned move looks sensible |
| 6 | `omni-autofocus -vv move-z 1` | **yes, 1 mm** | Z moves ~1 mm; sensor change ≈ +1.0 mm |
| 7 | `omni-autofocus move-z -1` | **yes** | returns; sensor change ≈ −1.0 mm |
| 8 | `omni-autofocus move-z 10` then `-10` | **yes** | sensor changes ≈ ±10 mm (checks 800 pulses/mm) |
| 9 | `omni-autofocus focus` | **yes** | ends with `In focus` |
| 10 | Burn a focus test in LightBurn without touching Z | — | best line at the autofocused height |

| 12 | With LightBurn connected: `status`, `height -n 3`, then use LightBurn | no | both work, LightBurn unaffected |
| 13 | `omni-autofocus focus-ladder`: burns at 0, −1, −2, +1, +2 mm from autofocus height | **yes** | 0 is sharpest (else set `focus.offset_mm`) |
| 11 | Create a second LightBurn BSL profile for lens A (70×70), select it, close LightBurn, run `omni-autofocus focus --dry-run` | no | prints "lens A … from last-used LightBurn profile" |

If step 6 moves the wrong way (sensor change ≈ −1 mm): set `invert_direction = true` in `[z_axis]`
and repeat 6–7. If the distance is off by a constant factor, check `pitch_pulse`/`screw_pitch`.
If the best focus in step 10 is not at the autofocused height, adjust `focus.offset_mm`.

Results log (fill in):

| date | step | result | notes |
|---|---|---|---|
| 2026-10-01 | 1 | pass | device opened at `vid_04b4&pid_1004` |
| 2026-10-01 | 2 | pass | framing, checksum and echo confirmed. Replies carry seq byte `80` and status byte `01` (bit 0 set, error bits 1–3 clear). AA05: payload 32 bytes, board state 0, free cache 32767 KiB. AA07: payload 44 bytes, `reply[0x28] = cc` → axes 0/1 nibble `0xc` (moving bit clear; meaning of bits 2–3 unknown, possibly limit/home inputs) |
| 2026-10-01 | 3 | partial | AAC1 pass-through and Modbus confirmed: request answered, collect returned `01 04 04 7f ff ff ff d3 d0` (valid CRC) = sensor "no target" value. Need a surface within range to get a real distance |
| 2026-10-01 | 3 | pass | with a surface under the head: 237.500 mm, identical over 10 readings (no jitter at all; sensor resolution may be coarse, check in step 4) |
| 2026-10-01 | 4 | pass | 36.5 mm block: 237.500 → 201.000 mm, difference exactly 36.5 mm. Units are mm, scale correct. Readings so far land on 0.5 mm steps |
| 2026-10-01 | 5 | pass | dry run planned +21.100 mm from 200.900 mm (so resolution is at least 0.1 mm, not 0.5) |
| 2026-10-01 | 6 | fail | list acked (`FA 01`) but Z did not move. Cause: the tool never put the board in the run state (`AA10 00 03`); AA05 byte 12 went 01 -> 00 (list held). Fixed by mirroring `MarkControl::doMoveAxisPulse` (reset, run, list, wait, reset) |
| 2026-10-01 | 6 | pass | with the run-state fix: Z moved; sensor 200.9 -> 202.1 mm (+1.2). Board state 3 while moving, finished flag set at the end. AA07 bytes 16-19 changed by exactly +800 = axis 1 position counter. 0.2 mm excess to be checked in step 8 (pitch 4 vs 5 mm?) |
| 2026-10-01 | 7 | pass | -1 mm twice: counter -800 each, sensor -1.2 and -0.9 mm |
| 2026-10-01 | 8 | pass | +10 / -10 mm: counter +-8000, sensor +10.1 / -10.1 mm. 800 pulses/mm (4 mm pitch) confirmed; sensor repeatability about +-0.2 mm |
| 2026-10-01 | 9 | pass | `focus`: 200.0 -> +22.0 mm move -> 222.4, correction -0.4 -> final 221.8 (error +0.2, within sensor noise). Added median-of-3 readings (focus.samples) afterwards to stop chasing noise |
| 2026-10-01 | 12 | partial | with LightBurn open and connected: `status` and `height -n 3` worked (221.8/221.8/221.7 mm). Board state 3 while LightBurn is connected = run state, so Z moves now restore the run state instead of resetting, and refuse to start while the controller reports unfinished work. LightBurn side still to confirm |
