# Changelog

## 1.0.0 (unreleased)

First public release. Verified on a ComMarker Omni Xe 6W (BSL controller, USB `04B4:1004`).

### Omni Autofocus app and installer

- one-click autofocus in a small always-on-top window next to LightBurn, plus a global hotkey
  (Ctrl+Alt+F, configurable as `app.hotkey`); light and dark theme following Windows
- lens picker (Auto / A / B; Auto follows the LightBurn profile), height check, fine-tuning, USB
  driver check, start with Windows, remembers its position
- moving up never asks; a move down of more than 10 mm (`app.confirm_down_above_mm`) asks first, with
  "No" as the default
- first use shows the focus heights read from the laser and saves them only after you accept
- per-user installer (no administrator rights): Start-menu shortcuts, optional start with Windows,
  closes a running app gracefully on upgrade/uninstall, refuses while a fine-tuning run uses the laser

### Focusing and safety

- measure with the built-in height sensor (median of 3 readings), move Z to the lens's focus height,
  re-check with a second pass; exits 3 with a warning if it ends more than 0.5 mm from focus
- every Z move is checked twice: against the controller's position counter, and by the sensor
  reading, which must change by roughly the commanded amount in the same direction
- until Z has been seen to follow, a move longer than 4 mm starts with a checked 3 mm probe and the
  rest is re-planned from the new reading, so a reversed axis travels at most about 4 mm the wrong
  way before it is caught; after a motion fault every move asks first
- refuses to move while LightBurn runs a job or its framing preview; coexists with a connected
  LightBurn; a cross-process lock keeps two Omni Autofocus programs from moving Z at once
- settings values are range-checked (no nan/inf; move limit at most 150 mm, the down-move
  question threshold at most 60 mm); focus heights are checked against the sensor range when used

### Calibration

- factory calibration read from the laser's flash on first use (`calibration` command,
  `config init --from-laser`); ComMarker Studio's files are used only when the laser holds no usable
  calibration; built-in example values are never used silently
- `focus-ladder`: dial in focus with test burns from -4..+4 mm (one-directional approach), then save
  the centre of the good range as the lens focus height
- `set-focus VALUE | --here | --factory`, `config set SECTION.KEY VALUE`
- automatic lens selection from the active LightBurn BSL profile's field size (fallback: ComMarker
  Studio's lens setting), always shown; `--lens` and the app's lens picker override it

### Command line and tools

- `focus`, `focus-ladder`, `height`, `move-z`, `status`, `devices`, `driver`, `calibration`,
  `set-focus`, `config`, `app`; `--simulate` runs everything against a protocol-level simulator
- `driver`: tells apart a missing laser, a missing driver and the wrong driver, explains the fix, and
  offers to run ComMarker's driver installer when it is on the PC or the laser's USB stick
- the console window stays open after a double-click or a shortcut (closes 5 s after success)
- Windows only for now (macOS and Linux are not supported)

### For LightBurn

- `docs/lightburn-kit/`: pitch, gap analysis against LightBurn's bundled BSL library, integration
  guide and byte-exact test vectors

MIT licence: free to use, please credit.
