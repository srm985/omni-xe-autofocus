# Integration guide

How to add autofocus for the ComMarker Omni X / Xe to an application that already drives its BSL
controller. Byte layouts are in [../protocol.md](../protocol.md); every frame below has an exact
example in [test-vectors.json](test-vectors.json). The reference implementation (`src/omni_autofocus/`) is named in brackets.

## 1. Read the height sensor  [`Controller.read_height_once`]

The sensor is a Modbus RTU device (slave 1) on the controller's RS-485 port.

1. Send `AAC1` with the 8-byte Modbus request `01 04 00 00 00 02 71 CB` (read input registers 0–1).
2. Wait about 200 ms.
3. Send an empty `AAC1`. Reply payload: `[2]` = number of bytes received, Modbus frame from `[4]`.
4. Check the Modbus CRC, then `distance_mm = BE u32(register bytes) / 1000`.
5. `0x7FFFFFFF` means **no surface in range** (the sensor measures about 120–280 mm and reports the
   same value for too near and too far). Tell the user to bring the head roughly to working height.

Readings wobble by about ±0.2 mm, so use the **median of three** readings. Retry communication
failures (the vendor tries up to 10 times), but never retry a "no surface" answer.

## 2. Compute the move  [`autofocus.plan`]

```
target = profile is lens B ? fBestFocalDistance_B : fBestFocalDistance   (+ optional user offset)
reject if reading outside [fMinDistanceOfSensor, fMaxDistanceOfSensor]   (120..280 mm)
move_mm = target - reading          (positive = head up, away from the work)
if |move_mm| < 0.1 mm: already in focus
if |move_mm| > limit (e.g. 60 mm): refuse
pulses = trunc(pitchPulse * move_mm / screwPitch)     (3200 * move / 4 = 800 pulses per mm)
```

The focus target is a **sensor reading**, not a lens-to-work distance. On the tested Omni Xe 6W:
222 mm for lens B (150×150 mm field) and 181 mm for lens A (70×70 mm). Other machines differ: read
them from the laser (section 4).

## 3. Move Z  [`Controller.move_axis`]

Z is **auxiliary axis 1**, moved with list command `0x03A0` on the data port. Encoding (slot layout,
speeds, the direction bit `(pulses < 0) XOR bRevRot`) is in protocol.md section 4; the `+1 mm`,
`-1 mm` and `+17 mm` examples in the vectors use the Omni's `axisZParExt` (3200 pulses/rev, 4 mm/rev,
`bRevRot = true`, run 8 mm/s, accel 5 mm/s², start 0).

Sequence (the vendor's `MarkControl::doMoveAxisPulse`):

1. Refuse if `AA05` reports unfinished work (`reply[12]` bit 0 clear): a job or the framing preview is
   running.
2. `AA10 00 01` (reset), wait 10 ms, `AA10 00 03` (run). **Lists only execute in the run state**: a
   list sent otherwise is acknowledged but held, and nothing moves (found on hardware).
3. Send the list `[0x03A0 move, 0x0A00 delay(1)]` on the data port (after the free-cache check).
4. Poll `AA05` until `reply[12]` bit 0 is set (or the board state is ≤ 1), also wait at least the
   computed travel time, then until the axis' moving bit in `AA07` clears.
5. `AA10 00 01` (reset), or back to run if the board was in the run state before (LightBurn itself
   keeps it there while connected).

Check the move with the axis position counter: `AA07` bytes 16–19 (axis 1, BE u32, origin
`0x40000000`) change by exactly the pulse count.

## 4. Read the factory calibration  [`flash.py`, `Session.factory_calibration`]

Each Omni's focus heights, field sizes and Z parameters are measured at the factory and stored in the
controller's flash as ComMarker's parameter file. Reading it means no user input and no guessing per
model.

1. `AAE0`: the flash user area's start and end address.
2. Read the 4 KiB store index at offset 0 (backup at 0x1000) in chunks of ≤ 480 bytes: `AAE4` (load
   length at address), poll `AAE1` until "buffer ready", `AAE5` (fetch). **Sleep about 1 ms before
   each `AAE1` poll**; polling at once can see the previous chunk's ready bit and fetch 0 bytes.
3. Find `./config/lcsparam.cfg` in the index, read it, check its CRC-64/ECMA-182.
4. Undo the layers: optional LZMA (store flag bit 0), then Qt `qCompress` (4-byte BE length + zlib),
   then the `.cfg` obfuscation (8-byte header, then XOR with the repeating key `"this is elfive\0\0"`).
5. The JSON holds `lmcPars.params[parName = "default"]` with `fBestFocalDistance` (lens A),
   `fBestFocalDistance_B` (lens B), `fMin/MaxDistanceOfSensor`, `galvoParam.workSize` /
   `galvo2Param.workSize` (field sizes), and `extMarkerPar.axisZParExt` (Z axis).

All four commands only read. Index and file layouts are in protocol.md section 5.

## 5. Safety checks worth keeping

A one-click Z move can drive the lens into the work. The reference implementation does all of these,
and each one came from something real:

| Check | Why |
|---|---|
| Median of 3 readings | ±0.2 mm sensor noise otherwise makes the correction pass chase noise |
| Range check and "no surface" handling | the sensor gives one value for both too near and too far |
| Refuse while a job or framing preview runs | the controller would interleave the move with the job |
| Verify the position counter after each move | catches a move the controller did not execute |
| After each move, the reading must change by roughly the commanded amount, **same direction** | a reversed Z direction on another machine would otherwise send the second pass further into the work |
| Until Z has been seen to follow, start a move over 4 mm with a checked 3 mm step, then re-plan the rest from the new reading | a reversed axis then goes at most about 4 mm the wrong way (the probe, or a short unprobed move); a badly wrong pitch is caught after the probe (e.g. 6 mm at twice the expected travel); clamp the rest so the total never exceeds the approved move |
| After a motion fault, ask before every move until a move checks out | the fault does not disappear with the next click |
| At most two passes, then report | never loop on a mechanical problem |
| Ask before a large downward move (e.g. > 10 mm) | up moves away from the work; down approaches it |
| Never use built-in example focus heights silently | read them from the laser, or ask |

## 6. Suggested UI

* **Autofocus** button and shortcut for BSL devices with a Z sensor (for example, enabled when the
  laser's calibration holds focus heights and the sensor answers).
* Focus target per device profile, pre-filled from the laser's calibration, editable, plus an offset.
* Status after each run: "In focus (moved +17.0 mm)", or a sentence that says what to do.
* Optional "autofocus before each job".

## 7. Testing

* Unit-test against [test-vectors.json](test-vectors.json).
* The reference implementation has a protocol-level simulator (`omni-autofocus --simulate …`) that
  answers like the controller.
* Hardware: the author can run beta builds on an Omni Xe 6W and send `-vv` USB frame dumps.
