# pibooth-wled-neopixel
 
A [pibooth](https://github.com/pibooth/pibooth) plugin that lights a NeoPixel/WS281x
strip or ring using a **separate ESP32 running [WLED](https://kno.wled.ge/)**,
connected to the Raspberry Pi over **USB** - not the SPI/PWM control this author's
other plugin, [pibooth-neopixel_spi](https://github.com/peteoheat/pibooth-neopixel_spi),
uses.
 
## How it's different from pibooth-neopixel_spi
 
| | pibooth-neopixel_spi | pibooth-wled-neopixel (this repo) |
|---|---|---|
| What drives the pixels | The Raspberry Pi itself, over SPI | A separate ESP32 running WLED |
| How pibooth talks to it | Direct SPI writes from Python | JSON commands over a USB-serial link |
| Effects engine | Implemented in Python on the Pi | WLED's own effects engine on the ESP32 |
| Good for | LEDs wired straight into the Pi | A booth where the LED ring/strip already has (or you want) its own WLED controller, e.g. shared with other projects, or wired somewhere the Pi's SPI bus can't reach |
 
Both plugins implement the same pibooth state hooks, so pick whichever matches your
wiring - don't enable both at once against the same strip.
 
## How it works
 
The ESP32 runs **completely stock, unmodified WLED firmware** - no custom sketch
needed. WLED already exposes its JSON API on the USB serial port it uses for
programming/debugging, in exactly the same form as its HTTP `/json/state` API
(see the [WLED serial interface docs](https://kno.wled.ge/interfaces/serial/) and
[JSON API docs](https://kno.wled.ge/interfaces/json-api/)). This plugin just opens
that serial port from the Pi and writes small JSON objects to it as pibooth moves
through its states - no WiFi required at all once WLED has been configured.
 
## Hardware & WLED setup (one-off)
 
1. Flash WLED onto the ESP32 and, over WiFi during initial setup, tell it how many
   LEDs you have, which GPIO pin drives the data line, and the colour order - all
   from the normal WLED web UI/app. **Do not use GPIO1 or GPIO3** for the LED data
   pin: those are the ESP32's UART0 pins used by the USB-serial connection, and WLED
   disables serial JSON responses (GPIO1) or all serial input (GPIO3) if they're
   reassigned to LED output.
2. Leave WLED's *Sync Interfaces > Serial* baud rate at its default (115200) unless
   you have a reason to change it - just make sure `[WLED] baudrate` in
   `pibooth.cfg` matches whatever you pick.
3. Connect the ESP32 to the Raspberry Pi 5 with a USB cable (power + the serial link
   both ride over the same cable).
4. On the Pi, find the serial device:
```bash
   ls /dev/serial/by-id/
```
   You'll normally see one entry, e.g. `usb-Silicon_Labs_CP2102_...-if00-port0`.
   Using the `/dev/serial/by-id/...` path (rather than `/dev/ttyUSB0`) in
   `pibooth.cfg` is strongly recommended, since `/dev/ttyUSB0`'s number can shift if
   another USB-serial device is ever plugged in.
5. Add your user to the `dialout` group so pibooth can open the port without root:
```bash
   sudo usermod -aG dialout $USER
```
   (log out/in, or reboot, for this to take effect)
 
## Installation

```bash
pip install pibooth-wled-neopixel
```

This also installs `pyserial`. Install it into the same Python environment as
pibooth. pibooth discovers installed plugins automatically, so there is nothing to
add to `[GENERAL] plugins`. Run `pibooth` once to regenerate `pibooth.cfg` with the
new `[WLED]` section, or run `pibooth --config` to edit it from the UI.

To upgrade: `pip install --upgrade pibooth-wled-neopixel`.

### Manual install (without pip)

Copy `pibooth_wled_neopixel.py` somewhere on the Pi, `pip install pyserial`, then
reference it from `pibooth.cfg`:

```ini
[GENERAL]
plugins = /home/pi/pibooth_wled_neopixel.py
```

(`plugins` accepts a comma-separated list.) Don't do both, or the plugin is loaded
twice.

## Configuration reference
 
All options live under `[WLED]` in `pibooth.cfg`.
 
### Connection & general
 
| Option | Default | Meaning |
|---|---|---|
| `port` | `auto` | Serial port of the ESP32 (`auto` to detect it, or an explicit path such as `/dev/serial/by-id/usb-...-if00`) |
| `baudrate` | `115200` | Must match WLED's *Sync Interfaces > Serial* baud rate |
| `serial_timeout` | `0.3` | Serial read/write timeout, seconds |
| `led_count` | `0` | Number of LEDs on the strip/ring (`0` = auto-detect from WLED at startup) |
| `segment_id` | `0` | WLED segment ID used for solid colours and the countdown's lit pixels |
| `countdown_bg_segment_id` | `1` | WLED segment ID used for the countdown's background pixels (must be free - see caveats below) |
| `brightness` | `180` | Master brightness sent once at startup, 0-255 |
| `transition_ms` | `400` | Crossfade time for state changes, ms (the capture stage is always instant) |
 
### Live preview countdown
 
While pibooth is in its preview/"get ready" state, the plugin can animate a bar
that shrinks (or grows) across the strip in real time, in sync with pibooth's own
`[WINDOW] preview_delay` setting - no separate duration to configure.
 
| Option | Default | Meaning |
|---|---|---|
| `preview_countdown` | `True` | Animate a live shrinking/growing bar during the preview delay |
| `preview_countdown_color` | `0,220,255` | Colour of the remaining-time part of the bar |
| `preview_countdown_off_color` | `0,20,40` | Colour of the elapsed-time part of the bar |
| `preview_countdown_direction` | `down` | `down` shrinks as time runs out, `up` grows |
| `preview_countdown_interval_ms` | `120` | How often the bar is refreshed over serial, ms |
 
### Per-stage colour & effect
 
Each pibooth state below has four options: `<stage>_color` (R,G,B), `<stage>_fx`
(a WLED effect, see below), `<stage>_sx` (effect speed, 0-255) and `<stage>_ix`
(effect intensity, 0-255) - e.g. `wait_color`, `wait_fx`, `wait_sx`, `wait_ix`.
Setting a stage's `fx` to `-1` leaves the LEDs untouched when pibooth enters that
state.
 
`<stage>_fx` accepts **either** a raw WLED effect ID number (in which case that
stage's own `_sx`/`_ix` are used exactly as set) **or** one of the named patterns
in the table below, ported over from this author's original
[pibooth-neopixel_spi](https://github.com/peteoheat/pibooth-neopixel_spi) plugin -
in which case `_sx`/`_ix` are set automatically to sensible values for that
pattern and the stage's own `_sx`/`_ix` options are ignored. A name that isn't
recognised (typo, etc.) logs a warning and falls back to Solid, so a bad config
value can never silently misbehave.
 
| Stage | pibooth hook | Default colour | Default fx | When it fires |
|---|---|---|---|---|
| `wait` | `state_wait_enter` | 40,20,0 | `breathe` | Idle / attract screen |
| `choose` | `state_choose_enter` | 0,90,200 | `solid` | Choosing the number of photos |
| `chosen` | `state_chosen_enter` | 0,180,120 | `solid` | Layout chosen, about to start |
| `preview` | `state_preview_enter` | 0,40,90 | `solid` | Preview / get ready - this is the countdown's *background* colour; `preview_countdown_color`/`preview_countdown_off_color` above take over as soon as the countdown starts |
| `capture` | `state_capture_enter` | 255,255,255 | `solid` | Applied **instantly** (no crossfade), so it's already at full brightness before the first shot of a multi-photo sequence and stays on for every shot |
| `processing` | `state_processing_enter` | 110,0,200 | `breathe` | Processing the photo(s) |
| `print` | `state_print_enter` | 0,200,90 | `solid` | Print choice |
| `finish` | `state_finish_enter` | 255,255,255 | `colorloop` | Thank you / finished |
| `failsafe` | `state_failsafe_enter` | 255,0,0 | `blink` | Something went wrong |
 
#### Named patterns
 
These are the pattern names accepted by any `<stage>_fx` option, each mapped onto
a native WLED effect (with a matching speed/intensity chosen to feel similar to
this plugin's SPI-driven predecessor). Aliases are alternate spellings that map
to the same effect - both work, they're just there so config files written for
the old plugin's pattern names still make sense:
 
| Pattern name | Alias | WLED effect ID | WLED's current display name | sx | ix |
|---|---|---|---|---|---|
| `solid` | | 0 | Solid | 0 | 0 |
| `blink` | | 1 | Blink | 120 | 128 |
| `breathe` | `pulse` | 2 | Breathe | 60 | 128 |
| `wipe` | `color_wipe` | 3 | Wipe | 120 | 128 |
| `colorloop` | | 8 | Colorloop | 140 | 200 |
| `rainbow` | | 9 | Rainbow | 140 | 200 |
| `theater_chase` | | 13 | Theater | 150 | 128 |
| `twinkle` | | 17 | Twinkle | 120 | 128 |
| `sparkle` | | 20 | Sparkle | 180 | 40 |
| `chase_rainbow` | `chase_multi` | 30 | Chase Rainbow | 150 | 128 |
| `comet` | | 41 | **Lighthouse** (see note below) | 180 | 100 |
| `fireworks` | | 42 | Fireworks | 150 | 128 |
| `fire` | | 45 | Fire Flicker | 150 | 128 |
| `gradient` | | 46 | Gradient | 60 | 128 |
| `ocean` | | 101 | Pacifica | 80 | 128 |
 
> **Note on `comet`:** WLED has renamed the display name of this effect to
> "Lighthouse" in current versions of the app/web UI - internally it's still the
> same effect (`FX_MODE_COMET`, ID 41) this plugin's `comet` pattern name refers
> to. If you go looking for "Comet" in your WLED effects list and can't find it,
> this is why - look for "Lighthouse" instead. Effect IDs themselves are stable
> across WLED versions (WLED only ever appends new effects, it never renumbers or
> reuses existing IDs), but display names occasionally change like this, so if a
> pattern doesn't look like you expect, verify by ID rather than by name.
 
**Not ported:** the original plugin's `attract_sequence` feature - cycling
through several different patterns with individual per-entry durations while
idle - is a distinct feature (pattern *sequencing*, on top of picking a single
pattern per stage) and hasn't been carried over here. Let the author know if
you'd like it added.
 
#### Verifying an effect ID/name for yourself
 
To see WLED's current effect list for your own device: open its web UI, go to
the Effects tab, and hover over any effect to see its ID; or, while still on
WiFi, fetch `http://<esp32-ip>/json/eff`, which returns the full list as a JSON
array indexed by effect ID (e.g. array index 41 is whatever ID 41 currently
displays as).
 
Each `<stage>_color` value is a comma-separated `R,G,B` triple by default, but you
can add a 4th value (`R,G,B,W`) to also drive the white channel on an RGBW strip
(e.g. SK6812) - `capture_color = 255,255,255,255` for instance. **This is always
logical R,G,B(,W), never the strip's physical wiring order.** WLED's JSON API
documents `col` as an "RGB(W) color", and separately has its own per-output
**Color Order** setting (LED Preferences page in the WLED UI/app, e.g. RGB, GRB,
BRG - most WS2812B strips are actually wired GRB even though people call them "RGB
strips") which WLED applies internally right before it writes bytes to the strip.
That remapping is entirely WLED's job - this plugin (like the WLED app, HTTP
calls, MQTT etc.) always sends plain logical RGB(W) and never needs to know or
compensate for how your strip is physically wired. If red and green ever look
swapped, fix the *Color Order* setting on the ESP32 in WLED itself, not the
colours in `pibooth.cfg`.
 
The shipped defaults above only use the pattern names `solid`, `breathe`,
`colorloop` and `blink` (WLED effect IDs 0, 2, 8 and 1), all part of WLED's
original effect set - but as the pattern table above shows, far more are
available, and any WLED effect can also be used by setting `<stage>_fx` directly
to its numeric ID (with `<stage>_sx`/`<stage>_ix` to taste) instead of a pattern
name. WLED only ever appends new effects to the end of the list, so existing IDs
(and presets built on them) never change meaning - open your ESP32's WLED web UI,
hover over the effects list to see each one's ID, and set the option to that
number.
 
## Caveats
 
- **The countdown uses two WLED segments.** While a preview countdown is running,
  the plugin repeatedly resizes segment `segment_id` (the lit portion) and
  `countdown_bg_segment_id` (the rest of the strip). If you've already set up other
  segments on the device for something else, pick free segment IDs for both
  options. Because these are edited live from outside the WLED app, the WLED
  app/web UI's own segment list may look momentarily out of sync while a countdown
  is running or just after one finishes - this is cosmetic and doesn't affect
  pibooth's control of the strip.
- **`led_count` auto-detection needs a working connection at pibooth startup.** If
  the ESP32 isn't plugged in yet, or detection otherwise fails, the plugin logs a
  warning and disables the live countdown (falling back to just the static
  `preview_color`) until you set `led_count` by hand.
- A flaky or unplugged ESP32 never crashes or interrupts the booth - every serial
  operation is guarded, logged, and retried in the background (throttled to once
  every 5 seconds) rather than raised.
## Troubleshooting
 
- **Nothing lights up / "no serial port found" in the log** - check
  `ls /dev/serial/by-id/`, set `[WLED] port` explicitly to that path, and confirm
  your user is in the `dialout` group.
- **Connects, but commands seem to do nothing** - double check the ESP32's LED data
  GPIO isn't set to pin 1 or 3 in WLED (see Hardware setup above), and that
  `baudrate` matches WLED's serial setting.
- **Countdown bar doesn't animate** - it needs a known `led_count`; check the log
  at startup for an auto-detect warning, or set `led_count` manually.
- Run with `pibooth -v` (or `pibooth-diag`) for verbose logging - every command
  this plugin sends (and every connection problem) is logged under the `wled:`
  prefix.
## Testing without hardware

Real behaviour needs a Raspberry Pi and an ESP32. To exercise the plugin without
hardware, replace `serial.Serial` with a fake that prints each line written to it,
then call the `state_*_enter` hooks in turn.

## Releasing (maintainers)

1. Bump `__version__` in `pibooth_wled_neopixel.py` and add a `CHANGELOG.md` entry.
2. Check locally: `python -m pip install build twine && python -m build && twine check dist/*`
3. Publish a GitHub release tagged `v<version>`. The `publish.yml` workflow builds
   and uploads to PyPI using trusted publishing.

One-off setup: on pypi.org add a (pending) trusted publisher for this repo with
workflow `publish.yml` and environment `pypi`, and create a `pypi` environment in
the GitHub repo settings. To upload manually instead, run `twine upload dist/*`.

## License
 
MIT - see [LICENSE](LICENSE).
 
