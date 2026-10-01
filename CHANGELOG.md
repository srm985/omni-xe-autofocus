# Changelog

## 1.0.0 (unreleased)

First public release. Verified on a ComMarker Omni Xe 6W (BSL controller, USB `04B4:1004`).

- `focus`: measure with the built-in height sensor and move Z to ComMarker Studio's focus height,
  with confirmation, dry run, a move safety limit and a second correction pass
- automatic lens selection from the active LightBurn BSL profile's field size (fallback: ComMarker
  Studio's lens setting), always printed; `--lens` overrides
- median of several sensor readings per measurement (the sensor wobbles about ±0.2 mm)
- every Z move is checked against the controller's own axis position counter
- `height`, `move-z`, `status`, `devices` and `config` commands
- settings import from an installed ComMarker Studio (`config init --from-commarker`)
- `--simulate` mode and a protocol-level simulator for hardware-free development
- standalone Windows `.exe`; double-clicking it runs autofocus
