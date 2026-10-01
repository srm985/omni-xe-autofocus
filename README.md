# omni-autofocus

Autofocus for the **ComMarker Omni X / Xe** without ComMarker Studio, so the laser can be used with
LightBurn. The tool reads the machine's built-in height sensor through the laser controller and moves
the motorised Z axis to the focus height, using the same rules as ComMarker Studio, plus a few
safety and accuracy improvements.

> **Status:** working on real hardware (Omni Xe 6W). See
> [docs/hardware-testing.md](docs/hardware-testing.md) for what has been verified.

## Quick start (no Python needed)

1. ComMarker Studio must have been installed once: it installs the USB driver the laser needs
   (the laser shows up as "Cypress FX2LP Sample Device").
2. Download `omni-autofocus.exe` from the Releases page.
3. Put your work piece under the head, then **close ComMarker Studio and make sure LightBurn is not
   talking to the laser** (see [Using it with LightBurn](#using-it-with-lightburn)).
4. Double-click `omni-autofocus.exe`. It measures, shows the planned move, asks for confirmation,
   moves Z and re-checks.

## Commands

```bash
omni-autofocus focus                # measure, confirm, move Z, re-check (the default when double-clicked)
omni-autofocus focus --dry-run      # only show the planned move
omni-autofocus height -n 5          # read the sensor (no motion)
omni-autofocus move-z 2.5           # relative Z move in mm (positive = larger sensor reading)
omni-autofocus status               # controller state and Z position counter
omni-autofocus --simulate focus     # try everything against a built-in simulator
```

`-y` skips confirmation, `--lens a|b` picks the lens, `-v`/`-vv` add logging and raw USB frame dumps.

## Lenses

The Omni takes two field lenses, and each has its own focus height. The focus height is stored as a
**sensor reading**, the distance the height sensor sees at best focus: 222 mm for lens B (150×150 mm
field) and 181 mm for lens A (70×70) on the tested machine. The tool picks the lens automatically and
prints its choice on every run:

1. `--lens a|b` (or `focus.lens` in the settings) wins if given.
2. Otherwise **LightBurn**: the field size of your BSL device profile (the last-used one, or the only
   one). With one LightBurn profile per lens, as LightBurn recommends for galvos, switching profiles
   in LightBurn switches the focus height.
3. Otherwise ComMarker Studio's lens setting ("Galvo B").

## Settings

Defaults match the tested Omni Xe 6W (focus heights above, Z on axis 1 at 800 pulses/mm). If your machine differs, copy the
values from your ComMarker Studio install:

```bash
omni-autofocus config init --from-commarker
omni-autofocus config show
```

Settings live in `%APPDATA%\omni-autofocus\config.toml`. You can override the location with
`--config` or `OMNI_AUTOFOCUS_CONFIG`. Useful keys:

| key | meaning |
|---|---|
| `focus.lens` | `"auto"` (default), `"a"` or `"b"` |
| `focus.field_a_mm`, `focus.field_b_mm` | lens field sizes used to recognise LightBurn profiles |
| `focus.offset_mm` | added to the focus distance: fine-tune focus, or defocus on purpose |
| `focus.max_move_mm` | refuse larger single moves (default 60) |
| `focus.samples` | sensor readings per measurement (median, default 3) |
| `z_axis.invert_direction` | flip Z direction if your machine moves the wrong way |

## Using it with LightBurn

Only one program should talk to the laser at a time. Run the autofocus while LightBurn is not
connected to the laser, then let LightBurn reconnect for the job. LightBurn does not need any Z
settings for this. Focus is set physically before the job.

## Troubleshooting

* **"sees no surface within its measuring range"**: the sensor only measures 120–280 mm and gives the
  same answer for too near and too far. Bring the head to roughly working height with the machine's
  Z buttons and try again.
* **"counter moved … expected …"**: the controller did not execute the full move (stall, limit).
  Check the Z axis mechanically before trying again.
* **No device found**: check the USB cable and power, and that ComMarker Studio has installed its
  driver (`omni-autofocus devices` lists what the driver sees).

## How it works

The height sensor is a Modbus RTU device on the controller's RS-485 port, reached with the
controller's `0xAAC1` pass-through command; Z is auxiliary axis 1, moved with the `0x03A0` list
command while the controller is in its run state. Full details: [docs/protocol.md](docs/protocol.md).
A proposal for native support in LightBurn: [docs/lightburn-proposal.md](docs/lightburn-proposal.md).

## Development

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
.venv\Scripts\ruff check src tests && .venv\Scripts\ruff format --check src tests
.venv\Scripts\python -m pip install -e ".[build]"; powershell -File tools\build_exe.ps1   # builds dist\omni-autofocus.exe
```

Layout: `framing.py` (USB frames) → `commands.py` (controller commands) → `controller.py`
(request/response, waits, verification) → `autofocus.py` / `cli.py`. `cyusb.py` is the Windows USB
transport (stdlib `ctypes` only), `simulator.py` a fake controller used by tests and `--simulate`.

## Legal

Independent, unofficial project, not affiliated with or endorsed by ComMarker, BSL or LightBurn
Software. It contains no code or binaries from those vendors. It exists so the laser can be used with other software (interoperability). Product names
belong to their owners.

Moving the Z axis can drive the lens into the work. Use the dry run, keep the safety limit and stay
at the machine. Provided as is, without warranty (see LICENSE).
