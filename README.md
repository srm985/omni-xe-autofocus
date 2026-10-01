# omni-autofocus

Autofocus for the **ComMarker Omni Xe** without ComMarker Studio, so the laser can be used with
LightBurn. The tool reads the machine's built-in height sensor through the laser controller and moves
the motorised Z axis to the focus height, using the same rules as ComMarker Studio.

> **Status:** protocol documented and fully covered by unit tests and a simulator, but **not
> yet tested on real hardware**. Follow [docs/hardware-testing.md](docs/hardware-testing.md) before
> relying on it.

## Requirements

* Windows, Python 3.11+
* The Cypress **CYUSB3** driver that ComMarker Studio installs (the laser shows up as
  "Cypress FX2LP Sample Device"). No other runtime dependencies.

## Install

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e .
```

## Use

Close ComMarker Studio and disconnect LightBurn from the laser first: only one program should talk to
the controller at a time.

```bash
omni-autofocus height               # read the sensor (no motion)
omni-autofocus focus --dry-run      # show what it would do
omni-autofocus focus                # measure, confirm, move Z, re-check
omni-autofocus move-z 2.5           # relative Z move in mm (positive = larger sensor reading)
omni-autofocus status               # controller state
omni-autofocus --simulate focus     # try everything against the built-in simulator
```

`-v`/`-vv` add logging / raw USB frame dumps. `--lens a|b` overrides the lens choice.

### Settings

Defaults match this machine's ComMarker Studio configuration (lens B / 150 mm field, focus at a
sensor reading of 222 mm; Z = axis 1, 800 pulses/mm). To copy the values from an installed ComMarker
Studio into a settings file:

```bash
omni-autofocus config init --from-commarker
omni-autofocus config show
```

The file lives at `%APPDATA%\omni-autofocus\config.toml` (override with `--config` or
`OMNI_AUTOFOCUS_CONFIG`). Useful keys: `focus.lens`, `focus.offset_mm` (focus fine-tune / defocus),
`focus.max_move_mm` (safety limit), `z_axis.invert_direction`.

## How it works

See [docs/protocol.md](docs/protocol.md). In short: the height sensor is a Modbus RTU device on the
controller's RS-485 port, reached with the controller's `0xAAC1` pass-through command; Z is auxiliary
axis 1, moved with the `0x03A0` list command.

## Development

```bash
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
.venv\Scripts\ruff check src tests && .venv\Scripts\ruff format --check src tests
```

Layout: `framing.py` (USB frames) → `commands.py` (controller commands) → `controller.py`
(request/response, waits) → `autofocus.py` / `cli.py`. `cyusb.py` is the Windows USB transport,
`simulator.py` a fake controller used by tests and `--simulate`.

## Disclaimer

Unofficial, not affiliated with ComMarker or BSL. Moving the Z axis can crash the lens into the work
or the end stops; use the dry run and the safety limit, and stay at the machine.
