# Hardware bring-up checklist

Status (2026-10-04): steps 1-9 and 11-22 pass on an Omni Xe 6W; step 10 (a LightBurn focus test) is
still to do. On a
new machine, go through the steps in order and stop at the first surprise. Run each command with
`-vv` the first time so the raw USB frames are printed; save the output if anything looks wrong.

Before every session: **close ComMarker Studio** (it polls the controller constantly). LightBurn may
stay open and connected (step 12), but do not run a job while the tool moves Z. Keep a hand near the
power switch for motion steps.

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
| 11 | Create a second LightBurn BSL profile for lens A (70×70), select it, close LightBurn, check the app's lens line | no | shows how Auto decides (see log) |
| 12 | With LightBurn connected: `status`, `height -n 3`, then use LightBurn | no | both work, LightBurn unaffected |
| 13 | `omni-autofocus focus-ladder`: burns from −4 to +4 mm at near-threshold power | **yes** | good range is centred on 0 (else the tool saves the centre) |
| 14 | `omni-autofocus calibration` | no | lists 181 / 222 mm, 70 / 150 mm fields, Z axis 1 at 800 pulses/mm |
| 15 | `set-focus` (value / `--here` / `--factory`), `config set`, `calibration` with a scratch settings file | no | values saved and compared as shown |
| 16 | Install `OmniAutofocus-Setup.exe`, open the app from the Start menu; first Autofocus | **yes** | first-use dialog lists 181 / 222 mm; after Yes: "In focus" |
| 17 | Hotkey Ctrl+Alt+F while LightBurn has focus | **yes** | autofocus runs, chime, "In focus" or "Already in focus" |
| 18 | Open LightBurn's framing preview, press Autofocus | no | "Stopped": the laser is busy |
| 19 | Lens picker A / B / Auto, ⋯ → Check height | no | lens line updates; height shown, nothing moves |
| 21 | ⋯ → Settings: Factory (both lenses), Use current at best focus, change the hotkey and Save, then press the new hotkey | no (Use current only reads) | values filled from the laser / sensor; new hotkey works without a restart |
| 20 | ⋯ → Fine-tune focus in the app: Start, burn each mark, press Next or Ctrl+Alt+F from LightBurn; try Next while a burn is still running; pick the good range | **yes** | each mark at the shown height; busy laser makes it wait; good range centred on 0 (step 10) |
| 22 | Raise the head out of the sensor's view (above 280 mm), press Autofocus, then jog down and briefly up with the machine's Z buttons | **yes** (after the head stops) | "Can't see the work", button reads Cancel, "Head moving" while jogging, Z does not move while waiting; in view: distance to focus and the right arrow both ways; the bar fills when the head holds still, then it focuses without asking |

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
| 2026-10-01 | 12 | pass | ladder run with LightBurn connected: LightBurn burned normally after every Z move. Return move was refused while LightBurn's framing preview was open (controller reports unfinished work) -> ladder now asks to close it and retries |
| 2026-10-01 | 13 | inconclusive | 0, -1, -2, +1, +2 mm marks hard to tell apart (galvo depth of field). Values match ComMarker's factory note card (222 / 181), so defaults kept. Ladder redesigned for dialling in: -4..+4 mm, one-directional approach, near-threshold power, lowest/highest good mark -> midpoint saved as the lens focus height |
| 2026-10-01 | 14 | pass | `calibration` read the factory values from the laser: 181/222 mm, 70/150 mm fields, Z axis 1 at 800 pulses/mm, reversed. Needed two fixes found on hardware: a second `qCompress` layer, and a 1 ms sleep before flash-state polls (otherwise an empty fetch) |
| 2026-10-01 | 15 | pass | scratch settings file: first run read the calibration from the laser; `--factory` read 222.0; `--here` measured 224.3 (head left 2 mm high by the ladder run); typed values for lens B and A saved independently; 35 mm rejected; `calibration` flagged the lens A change; `config set` validated keys; `focus --dry-run` used target + offset (222.2) |
| 2026-10-01 | 17 (early) | pass, unplanned | unplanned run: the laser was connected during an app test and the hotkey ran a real autofocus (net −1.8 mm, ended "In focus" at 222.0 mm). Since then app tests run in simulation only |
| 2026-10-01 | 16 | pass | real install (per-user, dark theme, hotkey registered). First Autofocus showed the first-use dialog, Yes saved the laser's calibration; moved up 11.9 mm (3 mm probe, then the rest) to 222.0 mm: "In focus", lens line "Auto → B · LightBurn 'BSLFiber' · 222.0 mm" |
| 2026-10-01 | 19 | pass | lens picker A / B / Auto updated the lens line; ⋯ → Check height showed the height without moving; reopened app kept its position and lens choice |
| 2026-10-01 | 17 | pass | Ctrl+Alt+F with LightBurn focused: a downward move over 10 mm asked first (as designed); after Yes moved down 11.8 mm to 222.1 mm, "In focus" |
| 2026-10-01 | 18 | pass | LightBurn framing preview running: Autofocus refused with "Stopped · The laser is busy …", Z did not move, no motion fault latched; after stopping the preview: moved up 2.4 mm to 222.0 mm, "In focus" |
| 2026-10-01 | 20 | pass | in-app fine-tuning on hardware: Start autofocused and went to −4 mm, nine marks burned with Next / the hotkey, Z returned to focus, result view worked; owner: "looks good". Found and fixed on the way: the result used the noisy post-autofocus reading (0..0 showed 221.9), now based on the aimed 222.0 |
| 2026-10-01 | 11 | pass, design changed | second BSL profile (70×70) selected, then LightBurn closed: prefs.ini rewritten on close but `DefaultDevice` stayed 0 (a JCZ profile). So LightBurn records its default device, not the one in use, and Auto cannot follow a profile switch. With two BSL profiles Auto now falls back to ComMarker Studio's lens setting (B here) and says so; the README tells users to pick A/B in the app when swapping lenses |
| 2026-10-01 | 5/20 | pass | fine-tuning result accepted by the owner ("looks good") |
| 2026-10-01 | 21 | pass | in-app Settings on hardware: Factory, Use current, hotkey change applied without restart, invalid input refused; owner: "looks good" |
| 2026-10-04 | – | pass | sensor window measured by stepping Z through its travel: last valid reading 278.3 mm at the top and 120.5 mm at the bottom (onto a 47 mm block), "no target" one step beyond each; readings follow Z 1:1. Z's limit switches stop the motor and show as an axis counter shortfall ("counter moved -126 pulses, expected ±1600") |
| 2026-10-04 | 22 | pass | head 65 mm above focus: "Can't see the work"; owner lowered it with the Z buttons, the app picked the block up at 275.1 mm once the head held still, asked before the 53 mm downward move, then probed 3 mm and finished at 221.9 mm |
| 2026-10-04 | – | pass | Z counter during the machine's own Z buttons (read-only log): it follows a jog live at 800 pulses/mm and stops with the head, but counts **down for both directions** (up 2 s: sensor +9.6 mm, counter −7453; down 2 s: sensor −10.9 mm, counter −8831). So the counter shows only whether the head moves; direction comes from the sensor |
| 2026-10-04 | 22 | pass | with the live panel and no downward question: picked up the block at 275.2 mm, probe to 272.1, focused at 222.0 mm without asking; direction shown correctly both ways; nothing logged as failed |
