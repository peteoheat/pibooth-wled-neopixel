# CLAUDE.md

## Project

`pibooth-wled-neopixel` is a single-module [pibooth](https://github.com/pibooth/pibooth) plugin (`pibooth_wled_neopixel.py`, MIT licensed, published on PyPI). It lights a NeoPixel/WS281x strip or ring by sending WLED JSON commands over a USB-serial link to a separate ESP32 running stock, unmodified WLED firmware. It is the WLED-based sibling of the author's SPI-driven plugin `pibooth-neopixel_spi`. Don't enable both against the same strip.

Files: `pibooth_wled_neopixel.py` (all code), `pyproject.toml` (packaging, version read from `__version__`, `pibooth` entry point), `README.md` (user docs and full config reference), `CHANGELOG.md`, `.github/workflows/publish.yml` (PyPI trusted publishing on GitHub release), `LICENSE`.

Note: there is no `test_plugin.py` in the repo.

## Running / testing

- No build step. Dependencies: `pibooth` and `pyserial` (`pip install pibooth pyserial`). pyserial is imported in a try/except, so the module loads without it.
- The plugin is loaded by pibooth via the `pibooth` entry point once pip-installed (or manually via `[GENERAL] plugins = /path/to/pibooth_wled_neopixel.py`). Run `pibooth -v` for verbose logs. Every log line from this plugin is prefixed `wled:`.
- There are no tests or linter config. Real behaviour needs a Raspberry Pi and an ESP32, so verify changes by reasoning about the JSON sent. A fake `serial.Serial` is the way to test without hardware.

## Architecture (`pibooth_wled_neopixel.py`)

- `PATTERNS`: name to `(wled_fx_id, sx, ix, wled_display_name)`. Includes aliases (`pulse`, `color_wipe`, `chase_multi`). `comet` is WLED effect 41, which WLED now displays as "Lighthouse".
- `STAGES` and `STAGE_DEFAULTS`: the 9 pibooth states (wait, choose, chosen, preview, capture, processing, print, finish, failsafe). Each has `<stage>_color`, `_fx`, `_sx` and `_ix` options. Default sx/ix are filled in from `PATTERNS`.
- `_resolve_fx(cfg, stage)`: `<stage>_fx` is either a numeric WLED effect ID (uses the stage's own sx/ix) or a pattern name (uses the pattern's sx/ix and ignores the stage's). An unknown name logs a warning and falls back to Solid. `fx = -1` means leave the LEDs untouched for that stage.
- `_parse_color`: colours are always **logical** R,G,B or R,G,B,W. Never reorder channels for strip wiring. WLED's own per-output Color Order setting does that.
- `WledSerialLink`: thread-safe (RLock) newline-terminated JSON writes. It auto-detects the port (matches known USB-serial chip names, then falls back to ttyUSB*/ttyACM*). Reopens are throttled to once every 5 s. `send()` never raises: errors are logged, the port is closed, and the next call retries.
- `WledNeopixel`: maps states to WLED commands. `configure()` reads config and `start()` does the open and handshake in a **background thread**, so pibooth's first paint isn't blocked. If `led_count` is 0 it auto-detects it via `{"v": true}`.
- Preview countdown: a daemon thread resizes two WLED segments, `segment_id` (lit) and `countdown_bg_segment_id` (background), to draw a shrinking or growing bar. Its duration is `[WINDOW] preview_delay`. `_bg_segment_active` tracks whether the bg segment exists. `stop_countdown()` deletes it (`start=stop=0`) so later stages start from a single clean segment. The countdown needs a known `led_count`.
- Hooks at the bottom: `pibooth_configure`, `pibooth_startup`, `pibooth_cleanup`, `state_*_enter`, plus `state_preview_exit` (stops the countdown). A module-level `_ctrl` singleton holds the controller.
  - Every hook is wrapped in `@_safe`, which logs and swallows exceptions. An exception escaping a hook would send pibooth into the failsafe state, so a WLED or serial fault must never break the booth. Keep new hooks wrapped.
  - `capture` and `failsafe` are applied with `instant=True` (transition 0).

## Conventions and gotchas

- WLED's `transition` field is in units of 100 ms (`_transition_units`).
- When you add or change a config option, update all three places: the `pibooth_configure` `add_option` call, the `configure()` reader, and the README config tables.
- When you add or change a pattern, update `PATTERNS` and the README "Named patterns" table.
- WLED effect IDs in `PATTERNS` were compiled from WLED source and community references, not read from a device. Keep them as stable numeric IDs (WLED only appends effects).
- Don't use GPIO1 or GPIO3 for the LED data line. They are the ESP32 UART0 pins used by the USB serial link.
- `attract_sequence` from the SPI plugin is intentionally not ported (the README says so).
- Style: Python 3, `.format()` strings (no f-strings), `LOGGER` from `pibooth.utils`, log messages prefixed `wled:`, comments explain why. `__version__` is the single source of the package version (`pyproject.toml` reads it); bump it and `CHANGELOG.md` for each release. The module filename must stay underscore-named so it is importable.
