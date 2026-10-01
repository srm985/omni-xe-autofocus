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
