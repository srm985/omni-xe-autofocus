# Changelog

## 1.0.1 (2026-10-04)

- when the sensor cannot see the work, Autofocus explains, waits while you move the head with the
  machine's Z buttons, and continues by itself once the work is in view and the head holds still;
  the button becomes Cancel (Esc and the hotkey cancel too). Z never moves while waiting.
- a live panel while you move the head: a gauge of what the sensor sees with the focus mark,
  whether the head is moving (and which way, once the work is in view), the distance to focus, and
  a bar that fills while the head holds still; a short sound when autofocus takes over. It waits
  for the Z counter to settle completely first, so jog counts never mix into the move's own check.
- no more question before a move down: pressing Autofocus is the go-ahead, as in ComMarker Studio.
  `app.confirm_down_above_mm` is retired (older settings files still load). After Z failed to
  follow a move, the app still asks before each move until one checks out.
- failures are logged (`-v`) with their reason
- a bad value in the settings file is reported as a settings-file problem that names the value,
  instead of a bare range message
- README: the sensor's 120–280 mm window (measured on hardware at both ends), materials it cannot
  see, and the sensor's sliding mount

## 1.0.0 (2026-10-01)

First public release. Verified on a ComMarker Omni Xe 6W (BSL controller, USB `04B4:1004`).

### Omni Autofocus app and installer

- one-click autofocus in a small always-on-top window next to LightBurn, plus a global hotkey
  (Ctrl+Alt+F, configurable as `app.hotkey`); light and dark theme following Windows
- lens picker (Auto / A / B; Auto follows the LightBurn profile), height check, USB driver check,
  start with Windows, remembers its position
- sharp at any display scaling: scaled check boxes, tidy Settings fields, long messages wrap
  instead of being cut off, and one window width in every view
- Settings in the window: focus heights per lens (type, use the current height, or restore the
  laser's factory value), hotkey (applies at once), finish sound, the down-move question threshold,
  focus nudge, and the Z direction switch; values are checked before saving
- messages no longer send app users to the command line
- fine-tuning with test burns in the window: big step display, Next button or the hotkey from
  LightBurn, pick the best marks, save; Stop returns Z to focus at any step
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
  rest is re-planned from the new reading (never beyond the approved move), so a reversed axis
  travels at most about 4 mm the wrong way; a stall, a lost reading or a wrong direction latches a
  motion fault, and every move then asks first
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
