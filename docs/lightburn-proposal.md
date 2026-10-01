# Proposal: native autofocus for ComMarker Omni X / Xe (BSL controller) in LightBurn

## Summary

ComMarker's Omni X/Xe galvo lasers have a motorised Z axis and a distance sensor on the head. LightBurn
already drives their BSL controller, but autofocus only works in ComMarker Studio, so owners switch
programs for every job. This project shows the feature needs only three controller commands, two of
which LightBurn's bundled BSL library already contains. A working, tested open-source reference
implementation is available: <!-- TODO: repository URL -->.

## What autofocus does on this machine

1. Read the height sensor: a Modbus RTU device on the controller's RS-485 port.
2. `move = target − reading`, where the target is a per-lens sensor reading stored in ComMarker
   Studio's config (222 mm for the 150 mm lens, 181 mm for the 70 mm lens on the tested machine).
3. Move the Z axis (auxiliary axis 1) by `move × 800 pulses/mm`, then optionally re-measure.

## Controller commands involved

All on the board protocol used by `Executor7` (USB `04B4:1004`). Full byte-level details:
[protocol.md](protocol.md).

| Purpose | Command | In LightBurn's bundled executor.dll? |
|---|---|---|
| RS-485 pass-through (sensor) | `0xAAC1` (`Executor7::setDataTransmit2`) | **No.** Only the older `setDataTransmit` (`0xAAC0`) is present; ComMarker Studio 1.0.17 ships a newer BSL library with `setDataTransmit2` |
| Run/reset state | `0xAA10` (`setRun` / `setReset`) | Yes |
| Z move | list command `0x03A0` (`sendAxisMovePulse`) | Yes |
| Axis position / moving flags | `0xAA07` (`getDevExtState`) | Yes |

Sensor exchange: send `AAC1` carrying `01 04 00 00 00 02 71 CB`, wait 200 ms, send an empty `AAC1`.
The reply carries `01 04 04 b0 b1 b2 b3 crc crc`, and the distance in mm is BE u32 / 1000. The value
`0x7FFFFFFF` means no surface in range.

A Z move only executes with the board in the run state: reset, run, list `[0x03A0, delay]`, wait for
finish, reset (as in ComMarker's `MarkControl::doMoveAxisPulse`).

## Machine values for the Omni Xe 6W

| Setting | Value | Source |
|---|---|---|
| Z axis | auxiliary axis 1, 3200 pulses/rev, 4 mm/rev (800 pulses/mm), reversed | `axisZParExt` in ComMarker's `lcsparam.cfg`; scale verified on hardware |
| Z speed | start 0, run 8 mm/s, accel 5 mm/s² | same |
| Focus targets | 222 mm (lens B, 150×150), 181 mm (lens A, 70×70) | `fBestFocalDistance(_B)` |
| Valid sensor range | 120–280 mm | `fMin/MaxDistanceOfSensor` |

Other Omni variants may differ. The values can be read from a ComMarker Studio install (the reference
tool's `config init --from-commarker` decodes the file).

## Suggested LightBurn integration

* Map the existing BSL "Enable Z" settings to axis 1 / 800 steps per mm for this machine profile.
* Add an "Auto-focus" action for BSL devices with a sensor: read height, compute the move, move Z,
  re-measure once. Expose per-lens target distances and an offset. LightBurn already knows the active
  lens (one device profile per lens), so the right target can follow the profile. The reference tool
  does this from outside by matching the profile's field size.
* Treat the sensor's ±0.2 mm noise with a median of a few readings, and verify the move with the
  axis position counter (`AA07` bytes 16–19).

## Verification

Hardware log: [hardware-testing.md](hardware-testing.md). The owner is happy to test a LightBurn beta
build on their machine.
