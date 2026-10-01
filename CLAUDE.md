# CLAUDE.md

Standalone autofocus for the ComMarker Omni Xe (BSL "Executor7" USB controller) so the owner can use
LightBurn. The controller protocol is documented in `docs/protocol.md`.

## Commands

- Tests: `.venv\Scripts\python -m pytest`
- Lint/format: `.venv\Scripts\ruff check src tests` / `.venv\Scripts\ruff format src tests`
- Hardware-free run: `.venv\Scripts\omni-autofocus --simulate -vv focus --dry-run`; the app:
  `.venv\Scripts\omni-autofocus --simulate app`
- Build exes + installer: `powershell -File tools\build_exe.ps1` (installer needs Inno Setup 6)
- After changing encoders: `.venv\Scripts\python tools\export_vectors.py` (LightBurn kit vectors;
  `tests/test_vectors.py` fails if stale)

## Rules

- Hardware status lives in `docs/hardware-testing.md` (all but steps 10-11 pass as of 2026-10-01).
  Never send motion commands to the real laser without the user's explicit go-ahead in chat. The
  laser is often connected: test the app and CLI with `--simulate` only (a hotkey or button press
  in real mode moves Z).
- Shared logic lives in `session.py` (settings, first-run calibration, lens, laser lock) and
  `autofocus.run` (the focus loop with its motion check); `cli.py` and `app.py` only present it.
  `app.py` touches Tk only on the main thread: workers post events to `App.events`.
- Golden byte vectors in `tests/` are the reference encodings. If hardware disagrees, update
  `docs/protocol.md` and the vectors together, citing the observed frames.
- Keep runtime dependency-free (stdlib + ctypes).
