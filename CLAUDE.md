# CLAUDE.md

Standalone autofocus for the ComMarker Omni Xe (BSL "Executor7" USB controller) so the owner can use
LightBurn. The controller protocol is documented in `docs/protocol.md`.

## Commands

- Tests: `.venv\Scripts\python -m pytest`
- Lint/format: `.venv\Scripts\ruff check src tests` / `.venv\Scripts\ruff format src tests`
- Hardware-free run: `.venv\Scripts\omni-autofocus --simulate -vv focus --dry-run`

## Rules

- Hardware status lives in `docs/hardware-testing.md`; anything not ticked there is unverified.
  Never send motion commands to the real laser without the user's explicit go-ahead in chat.
- Golden byte vectors in `tests/` are the reference encodings. If hardware disagrees, update
  `docs/protocol.md` and the vectors together, citing the observed frames.
- Keep runtime dependency-free (stdlib + ctypes).
