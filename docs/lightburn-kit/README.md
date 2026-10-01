# Native autofocus for the ComMarker Omni X / Xe in LightBurn: integration kit

**For:** LightBurn Software. **From:** Sean (github.com/srm985), an Omni Xe owner.
**Licence:** MIT, free to use. Please credit this work if you use it.

## The pitch

ComMarker's Omni X and Omni Xe are galvo lasers with a motorised Z axis and a height sensor in the
head. LightBurn already drives their BSL controller, but autofocus only works in ComMarker Studio, so
owners switch programs, or focus by hand, for every new work piece.

Autofocus needs very little: **read the sensor, compute the move, move Z, check.** This kit contains
everything needed to add it to LightBurn. The protocol, the Z moves and the calibration read are
verified on a real Omni Xe 6W; see "Status of the evidence" for what is still pending:

* the exact controller commands, with byte-exact test vectors;
* the per-machine focus calibration, which the factory stores **in the laser itself**, so users type
  nothing in;
* a working, tested reference implementation that runs next to LightBurn today
  (in this kit under `src/`, and at https://github.com/srm985/omni-xe-autofocus; MIT);
* the safety checks that a one-click Z move needs;
* a hardware owner who is happy to test LightBurn beta builds.

## What a user would see

* An **Autofocus** button (and a shortcut) for BSL devices that have a Z sensor, next to the existing
  Z controls. One click: measure, move, re-check, "In focus".
* The focus height follows the **device profile**. LightBurn already recommends one profile per galvo
  lens, and each Omni lens has its own focus height in the laser's calibration.
* Optional: a per-profile focus offset for deliberate defocus, and an "autofocus before job" option.

## What LightBurn needs

| Piece | Controller command | In LightBurn's bundled BSL `executor.dll` (LightBurn 2.1.04, see note below) |
|---|---|---|
| Read the height sensor | `0xAAC1` RS-485 pass-through (`Executor7::setDataTransmit2`) | **No.** Only the older `setDataTransmit` (`0xAAC0`) |
| Move Z | list command `0x03A0` (`sendAxisMovePulse`) in the run state (`0xAA10`) | Yes |
| Confirm a move | `0xAA07` axis position counter and moving flag (`getDevExtState`) | Yes |
| Read the factory calibration | `0xAAE0`/`AAE1`/`AAE4`/`AAE5` flash reads (`readFlashData`, `getFlashInfo`) | **No** |

How this was checked: the exported symbol names in LightBurn 2.1.04's `executor.dll` (SHA-256
`17a9454c…5f03a6f`) were compared with those of ComMarker Studio's `executor.dll` (`6b6c3239…908bf61b`).

ComMarker Studio ships a newer build of the same BSL library that has all of these. So the simplest
route is probably **a library update from BSL**; sending the few raw commands yourselves is the
alternative, and [integration-guide.md](integration-guide.md) has every byte for that.

## Contents

| File | What it is |
|---|---|
| [integration-guide.md](integration-guide.md) | Step-by-step: sensor read, Z move, calibration, safety checks, suggested UI |
| [test-vectors.json](test-vectors.json) | Byte-exact frames and payloads to unit-test an implementation |
| [../protocol.md](../protocol.md) | The full protocol notes: framing, every field, what was verified |
| [../hardware-testing.md](../hardware-testing.md) | What was verified on the machine, step by step |
| [../../src/omni_autofocus/](../../src/omni_autofocus/) | Reference implementation (Python, standard library only) |

## Status of the evidence

Verified on an Omni Xe 6W (USB `04B4:1004`, firmware as shipped in 2026): framing and checksums,
state queries, the sensor pass-through and its "no target" value, Z moves (direction, 800 pulses/mm,
position counter), the run-state requirement, the flash calibration read (including two quirks found
only on hardware), running alongside a connected LightBurn, and the Windows app and installer around
the engine (including fine-tuning with test burns). Pending on hardware: a LightBurn test burn
confirming focus quality at the autofocused height (a first ladder test was inconclusive because the
galvo's depth of field is generous). Lens selection from outside LightBurn cannot follow profile
switches, because LightBurn records its default device rather than the one in use; inside LightBurn
the active profile is known, so this limitation does not apply. Not yet tested: the Omni X (non-Xe)
and other Omni variants, which may use other focus heights (the calibration read handles that) or Z
parameters (also in the calibration).

## Contact and credit

Sean, [github.com/srm985](https://github.com/srm985). Happy to answer questions, review an implementation against the hardware,
and test beta builds on an Omni Xe 6W.

This is independent work, not affiliated with ComMarker or BSL. It contains no vendor code and
exists so the laser can be used with other software (interoperability).
