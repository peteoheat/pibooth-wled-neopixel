# -*- coding: utf-8 -*-
"""pibooth-wled-neopixel

A pibooth plugin that drives a separate ESP32 running WLED (connected to the
Raspberry Pi over USB) to control a NeoPixel/WS281x strip or ring, instead of
driving the pixels directly from the Pi's own SPI/PWM bus.

The ESP32 runs stock, unmodified WLED firmware. WLED exposes its normal JSON
API (the same one normally used over HTTP/WiFi) directly on its USB serial
port, so this plugin never needs the ESP32 to be on the same WiFi network as
the booth -- it just writes small JSON commands down the USB-serial link.

See README.md for wiring, WLED setup and the full configuration reference.

Author: peteoheat
"""

import functools
import json
import threading
import time

import pibooth
from pibooth.utils import LOGGER

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pyserial is an optional dependency until this plugin is enabled
    serial = None
    list_ports = None

__version__ = "1.0.0"


# --- Named effect patterns --------------------------------------------------
#
# Each ``<stage>_fx`` option accepts either a plain WLED effect ID number (as
# before - full manual control, paired with the stage's own ``_sx``/``_ix``
# options), or one of the names below, which is a shorthand for "use this
# effect ID with these speed/intensity values" - WLED's own effect engine
# then renders it entirely on the ESP32; nothing is streamed frame-by-frame
# over the serial link. This is the WLED-native equivalent of the pattern
# library in pibooth-neopixel_spi (rainbow, comet, fire, sparkle, ...), which
# instead pushed every frame from the Pi over SPI - that approach doesn't
# translate to a serial JSON link, so these names map onto WLED's closest
# built-in effects rather than re-implementing the pixel math in Python.
#
# WLED's project policy is to only ever append new effects, never renumber
# or reuse an existing ID, so once one of these is confirmed correct for
# your WLED version it stays correct across updates. That said, these IDs
# were compiled from WLED's own source/community references rather than
# read from your specific device - if a name ever looks wrong, the sure way
# to check is your ESP32's WLED web UI/app effect list (or, while it's on
# WiFi, http://<esp32-ip>/json/eff, which returns the exact list for your
# firmware in ID order), and you can always fall back to a plain numeric ID.
#
# name            -> (fx id, default sx, default ix, WLED's own name for it)
PATTERNS = {
    "solid":         (0,   0,   0,   "Solid"),
    "blink":         (1,   120, 128, "Blink"),
    "breathe":       (2,   60,  128, "Breathe"),
    "pulse":         (2,   60,  128, "Breathe"),           # alias for "breathe"
    "wipe":          (3,   120, 128, "Wipe"),
    "color_wipe":    (3,   120, 128, "Wipe"),               # alias for "wipe"
    "colorloop":     (8,   140, 200, "Colorloop"),
    "rainbow":       (9,   140, 200, "Rainbow"),
    "theater_chase": (13,  150, 128, "Theater"),
    "twinkle":       (17,  120, 128, "Twinkle"),
    "sparkle":       (20,  180, 40,  "Sparkle"),
    "chase_rainbow": (30,  150, 128, "Chase Rainbow"),
    "chase_multi":   (30,  150, 128, "Chase Rainbow"),      # alias for "chase_rainbow"
    "comet":         (41,  180, 100, "Lighthouse"),         # WLED's internal name is still FX_MODE_COMET
    "fireworks":     (42,  150, 128, "Fireworks"),
    "fire":          (45,  150, 128, "Fire Flicker"),
    "gradient":      (46,  60,  128, "Gradient"),
    "ocean":         (101, 80,  128, "Pacifica"),
}


def _resolve_fx(cfg, stage):
    """Read ``<stage>_fx`` (and, for a plain numeric ID, ``<stage>_sx``/
    ``<stage>_ix``) and return (fx, sx, ix). A name from PATTERNS is a
    shorthand for its own recommended sx/ix -- use a numeric ID directly for
    full manual control over speed/intensity."""
    raw = cfg.get('WLED', '{}_fx'.format(stage)).strip()
    try:
        fx = int(raw)
        sx = cfg.getint('WLED', '{}_sx'.format(stage))
        ix = cfg.getint('WLED', '{}_ix'.format(stage))
        return fx, sx, ix
    except ValueError:
        pass

    pattern = PATTERNS.get(raw.lower())
    if pattern is None:
        LOGGER.warning("wled: '%s_fx' has unknown value %r (not a number and not one of "
                        "%s); using Solid for this stage", stage, raw, ", ".join(sorted(PATTERNS)))
        return 0, 0, 0
    fx, sx, ix, _wled_name = pattern
    return fx, sx, ix


STAGES = ("wait", "choose", "chosen", "preview", "capture",
          "processing", "print", "finish", "failsafe")

def _parse_color(value, fallback=(255, 255, 255)):
    """Turn a config value into a WLED colour tuple.

    WLED's JSON API always takes colours as *logical* (R, G, B) or
    (R, G, B, W) values -- see https://kno.wled.ge/interfaces/json-api/,
    which documents "col" as "an array of 3 or 4 bytes, which represents an
    RGB(W) color". The physical wiring order of a strip (e.g. a WS2812B
    strip that is actually wired GRB, which is most of them) is a separate,
    per-output "Color Order" setting in WLED's own LED Preferences page
    (https://kno.wled.ge/features/settings/#led-preferences) that WLED
    applies right before it writes bytes to the strip. That remapping is
    entirely WLED's responsibility and happens after this plugin's JSON
    command has already been parsed -- this plugin (and any other JSON API
    client: the WLED app, HTTP calls, MQTT, ...) never needs to know or
    care how the strip is physically wired, and must never try to reorder
    channels itself to compensate for it.

    So a `<stage>_color` config value is simply "R,G,B" for a plain RGB
    strip, or "R,G,B,W" to also drive the white channel on an RGBW strip
    (e.g. SK6812) -- WLED accepts either length directly.
    """
    if isinstance(value, (tuple, list)) and len(value) in (3, 4) \
            and all(isinstance(c, int) and 0 <= c <= 255 for c in value):
        return tuple(value)
    LOGGER.warning("wled: invalid colour value %r (expected 3 comma-separated "
                    "R,G,B integers 0-255, or 4 for R,G,B,W); using %r instead",
                    value, fallback)
    return fallback


# Each stage's shipped default names a PATTERNS entry (not a bare fx id) so
# the generated pibooth.cfg is self-documenting and doubles as a worked
# example of the new named-pattern feature. Visually these resolve to
# exactly the same WLED effect ids as before this feature existed.
STAGE_DEFAULTS = {
    "wait":       {"color": "40,20,0",     "fx": "breathe",
                   "desc": "idle / attract screen"},
    "choose":     {"color": "0,90,200",    "fx": "solid",
                   "desc": "choosing the number of photos"},
    "chosen":     {"color": "0,180,120",   "fx": "solid",
                   "desc": "layout chosen, about to start"},
    "preview":    {"color": "0,40,90",     "fx": "solid",
                   "desc": "preview / get ready (background colour behind the countdown)"},
    "capture":    {"color": "255,255,255", "fx": "solid",
                   "desc": "capture (always applied instantly, acts as fill light)"},
    "processing": {"color": "110,0,200",   "fx": "breathe",
                   "desc": "processing the photo(s)"},
    "print":      {"color": "0,200,90",    "fx": "solid",
                   "desc": "print choice"},
    "finish":     {"color": "255,255,255", "fx": "colorloop",
                   "desc": "thank you / finished"},
    "failsafe":   {"color": "255,0,0",     "fx": "blink",
                   "desc": "error / failsafe"},
}
for _stage, _defaults in STAGE_DEFAULTS.items():
    _fx, _sx, _ix, _wled_name = PATTERNS[_defaults["fx"]]
    _defaults["sx"], _defaults["ix"] = _sx, _ix
del _stage, _defaults, _fx, _sx, _ix, _wled_name


# --- Low level: JSON-over-serial link to WLED -------------------------------

class WledSerialLink(object):
    """Thread-safe JSON-over-serial link to a WLED controller.

    WLED parses the same JSON documents on its serial port as it does on its
    HTTP ``/json/state`` endpoint (see https://kno.wled.ge/interfaces/serial/
    and https://kno.wled.ge/interfaces/json-api/). No special framing is
    required: a JSON object is written to the port and WLED parses it as soon
    as it has a complete document.
    """

    _KNOWN_USB_SERIAL_HINTS = ("CP210", "CH340", "CH910", "FTDI", "FT232",
                                "USB Serial", "Silicon Labs", "wchusb", "USB-SERIAL")

    def __init__(self, port, baudrate, timeout):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser = None
        self._lock = threading.RLock()
        self._last_open_attempt = 0.0
        self._reconnect_interval = 5.0

    def _resolve_port(self):
        """Return an explicit port path, or try to find the ESP32 automatically."""
        if self.port and self.port.strip().lower() != "auto":
            return self.port.strip()

        if list_ports is None:
            return None

        try:
            ports = list(list_ports.comports())
        except Exception:
            LOGGER.exception("wled: failed to list serial ports")
            return None

        # Prefer a device whose description/manufacturer matches a common
        # USB-serial bridge chip used on ESP32 dev boards.
        for info in ports:
            haystack = " ".join(filter(None, (info.description, info.manufacturer)))
            if any(hint.lower() in haystack.lower() for hint in self._KNOWN_USB_SERIAL_HINTS):
                return info.device

        # Fall back to any ttyUSB*/ttyACM* looking device.
        for info in ports:
            if "ttyUSB" in info.device or "ttyACM" in info.device:
                return info.device

        return None

    def open(self):
        """(re)open the serial port. Throttled so a missing/unplugged
        controller doesn't get hammered with open attempts every hook call."""
        with self._lock:
            if self._ser is not None and self._ser.is_open:
                return True

            now = time.monotonic()
            if now - self._last_open_attempt < self._reconnect_interval:
                return False
            self._last_open_attempt = now

            if serial is None:
                LOGGER.error("wled: pyserial is not installed - run 'pip install pyserial'")
                return False

            port = self._resolve_port()
            if not port:
                LOGGER.warning("wled: no serial port found for the WLED/ESP32 controller "
                                "(set [WLED] port explicitly if auto-detection keeps failing)")
                return False

            try:
                self._ser = serial.Serial(port, self.baudrate,
                                           timeout=self.timeout, write_timeout=self.timeout)
                # Give the ESP32 a moment to finish its USB-CDC/boot reset
                # before talking to it, and drop anything it printed at boot.
                time.sleep(0.3)
                self._ser.reset_input_buffer()
                LOGGER.info("wled: connected to %s at %s baud", port, self.baudrate)
                return True
            except Exception:
                LOGGER.exception("wled: failed to open serial port '%s'", port)
                self._ser = None
                return False

    def close(self):
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None

    def send(self, command, expect_reply=False, reply_timeout=None):
        """Send a JSON command (a dict) to WLED.

        Returns the parsed reply dict if ``expect_reply`` is True and a valid
        JSON reply could be read back within the timeout, else None. Never
        raises: connection problems are logged and swallowed so a flaky/
        unplugged ESP32 never interrupts the photobooth itself.
        """
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                if not self.open():
                    return None

            try:
                payload = json.dumps(command, separators=(",", ":")) + "\n"
                self._ser.reset_input_buffer()
                self._ser.write(payload.encode("utf-8"))
                self._ser.flush()
            except Exception:
                LOGGER.exception("wled: serial write failed, will try to reconnect")
                self.close()
                return None

            if not expect_reply:
                return None

            deadline = time.monotonic() + (reply_timeout if reply_timeout else self.timeout)
            buf = b""
            try:
                while time.monotonic() < deadline:
                    chunk = self._ser.read(256)
                    if chunk:
                        buf += chunk
                        try:
                            return json.loads(buf.decode("utf-8", "ignore"))
                        except ValueError:
                            continue  # incomplete JSON so far, keep reading
                    else:
                        time.sleep(0.01)
            except Exception:
                LOGGER.exception("wled: serial read failed")
            return None


# --- High level: pibooth-state -> WLED command mapping ---------------------

class WledNeopixel(object):
    """Maps pibooth states onto WLED JSON commands, and drives the live
    preview countdown from a background thread."""

    def __init__(self):
        self.link = None
        self.led_count = 0
        self.segment_id = 0
        self.bg_segment_id = 1
        self.transition_ms = 400
        self.stage_cfg = {}

        self.countdown_enabled = True
        self.countdown_color = (0, 220, 255)
        self.countdown_off_color = (0, 20, 40)
        self.countdown_direction = "down"
        self.countdown_interval = 0.12

        self._countdown_thread = None
        self._countdown_stop = threading.Event()
        # Tracks whether WLED segment bg_segment_id currently exists (it's
        # created lazily, the first time the countdown bar needs to split
        # off a background portion) - so stop_countdown() only ever tries to
        # delete it when there's actually something to clean up, rather than
        # sending a delete for a segment id WLED has never been told about.
        self._bg_segment_active = False

    # -- setup ---------------------------------------------------------

    def configure(self, cfg):
        self.link = WledSerialLink(cfg.get('WLED', 'port'),
                                    cfg.getint('WLED', 'baudrate'),
                                    cfg.getfloat('WLED', 'serial_timeout'))
        self.segment_id = cfg.getint('WLED', 'segment_id')
        self.bg_segment_id = cfg.getint('WLED', 'countdown_bg_segment_id')
        self.transition_ms = cfg.getint('WLED', 'transition_ms')
        self.brightness = max(0, min(255, cfg.getint('WLED', 'brightness')))
        self.led_count = max(0, cfg.getint('WLED', 'led_count'))

        self.countdown_enabled = cfg.getboolean('WLED', 'preview_countdown')
        self.countdown_color = _parse_color(cfg.gettyped('WLED', 'preview_countdown_color'),
                                             fallback=(0, 220, 255))
        self.countdown_off_color = _parse_color(cfg.gettyped('WLED', 'preview_countdown_off_color'),
                                                  fallback=(0, 20, 40))
        self.countdown_direction = cfg.get('WLED', 'preview_countdown_direction').strip().lower()
        interval_ms = cfg.getint('WLED', 'preview_countdown_interval_ms')
        self.countdown_interval = max(0.03, interval_ms / 1000.0)

        self.stage_cfg = {}
        for stage in STAGES:
            color = _parse_color(cfg.gettyped('WLED', '{}_color'.format(stage)))
            fx, sx, ix = _resolve_fx(cfg, stage)
            self.stage_cfg[stage] = (color, fx, sx, ix)

    def start(self):
        """Kick off the initial WLED connection/handshake in the background.

        pibooth_startup() runs on pibooth's own main thread, before its
        window has pumped any events or done its first draw - so anything
        slow done here directly delays that first paint (this is why the
        window used to stay blank until the first click: whatever forces a
        redraw). Opening the serial port takes ~0.3s, and led_count
        auto-detection waits up to 2s for a reply, so both are done from a
        background thread instead of blocking pibooth's startup on them.
        """
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self):
        if not self.link.open():
            LOGGER.warning("wled: controller not reachable at startup, "
                            "will keep retrying in the background")
        self.link.send({"on": True, "bri": self.brightness,
                         "transition": self._transition_units(self.transition_ms)})
        if self.led_count <= 0:
            self._detect_led_count()

    def _detect_led_count(self):
        reply = self.link.send({"v": True}, expect_reply=True, reply_timeout=2.0)
        try:
            count = int(reply["info"]["leds"]["count"])
            if count > 0:
                self.led_count = count
                LOGGER.info("wled: auto-detected %d LEDs", count)
                return
        except Exception:
            pass
        LOGGER.warning("wled: could not auto-detect the LED count from WLED; "
                        "set [WLED] led_count manually to enable the live preview countdown")

    @staticmethod
    def _transition_units(ms):
        """WLED's 'transition' field is in units of 100 ms."""
        return max(0, int(round(ms / 100.0)))

    # -- stage colours ---------------------------------------------------

    def apply_stage(self, stage, instant=False):
        cfg_tuple = self.stage_cfg.get(stage)
        if not cfg_tuple:
            return
        color, fx, sx, ix = cfg_tuple
        if fx < 0:
            return  # this stage is disabled - leave the LEDs as they are

        stop = self.led_count if self.led_count > 0 else 255
        command = {
            "transition": 0 if instant else self._transition_units(self.transition_ms),
            "seg": [{
                "id": self.segment_id,
                "start": 0,
                "stop": stop,
                "on": True,
                "col": [list(color)],
                "fx": fx,
                "sx": sx,
                "ix": ix,
            }],
        }
        self.link.send(command)

    def off(self):
        self.stop_countdown()
        self.link.send({"on": False, "transition": self._transition_units(self.transition_ms)})

    # -- live preview countdown ------------------------------------------

    def start_countdown(self, duration_seconds):
        self.stop_countdown()
        if not (self.countdown_enabled and self.led_count > 0 and duration_seconds > 0):
            return
        self._countdown_stop.clear()
        self._countdown_thread = threading.Thread(
            target=self._countdown_worker, args=(float(duration_seconds),), daemon=True)
        self._countdown_thread.start()

    def stop_countdown(self):
        self._countdown_stop.set()
        thread = self._countdown_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._countdown_thread = None
        # The countdown animates by resizing two segments (segment_id for the
        # lit portion, bg_segment_id for the dark portion) - whatever split
        # was last sent stays in effect until something explicitly changes
        # it. apply_stage() only ever touches segment_id, so without this,
        # bg_segment_id could be left behind covering part of the strip with
        # the last countdown frame's dim "off" colour, and (since it's a
        # higher segment id, drawn after/over segment_id) visibly override
        # part of every later stage's colour until the next countdown
        # happens to fully re-cover it. Deleting it here (start >= stop
        # removes a WLED segment) guarantees every stage after a countdown
        # starts from a single clean segment spanning the whole strip.
        #
        # Only do this if bg_segment_id was actually created this session -
        # it's created lazily by _send_countdown_frame() the first time the
        # bar needs to show a background portion, so on e.g. the very first
        # countdown ever run it may not exist yet, and sending a delete for
        # a segment id WLED has never seen isn't a well-defined no-op on
        # every WLED version, so it's best avoided rather than relied on.
        if self._bg_segment_active:
            self.link.send({"seg": [{"id": self.bg_segment_id, "start": 0, "stop": 0}]})
            self._bg_segment_active = False

    def _countdown_worker(self, duration):
        """Runs in a background thread for the whole preview delay, resizing
        two WLED segments (lit / background) to draw a shrinking or growing
        countdown bar - pibooth's own state_preview_do call blocks for the
        entire delay in the core camera plugin, so this can't be driven from
        a hookimpl running on the main thread."""
        start = time.monotonic()
        last_lit = None
        try:
            while not self._countdown_stop.is_set():
                elapsed = time.monotonic() - start
                remaining_fraction = max(0.0, min(1.0, 1.0 - elapsed / duration))
                if self.countdown_direction == "up":
                    lit = int(round((1.0 - remaining_fraction) * self.led_count))
                else:
                    lit = int(round(remaining_fraction * self.led_count))
                lit = max(1, min(self.led_count, lit))

                if lit != last_lit:
                    self._send_countdown_frame(lit)
                    last_lit = lit

                if elapsed >= duration:
                    break
                time.sleep(self.countdown_interval)
        except Exception:
            LOGGER.exception("wled: preview countdown worker failed")

    def _send_countdown_frame(self, lit):
        segments = [{
            "id": self.segment_id,
            "start": 0,
            "stop": lit,
            "on": True,
            "fx": 0,
            "col": [list(self.countdown_color)],
            "transition": 0,
        }]
        if lit < self.led_count:
            segments.append({
                "id": self.bg_segment_id,
                "start": lit,
                "stop": self.led_count,
                "on": True,
                "fx": 0,
                "col": [list(self.countdown_off_color)],
                "transition": 0,
            })
            self._bg_segment_active = True
        elif self._bg_segment_active:
            # Hide/delete the background segment while the bar is fully lit
            # (only if it's actually there to delete).
            segments.append({"id": self.bg_segment_id, "start": 0, "stop": 0})
            self._bg_segment_active = False
        self.link.send({"seg": segments})

    # -- teardown ----------------------------------------------------------

    def cleanup(self):
        self.stop_countdown()
        try:
            self.link.send({"on": False, "transition": 0})
        finally:
            self.link.close()


# Single controller instance for the lifetime of the pibooth process.
_ctrl = WledNeopixel()


def _safe(func):
    """Never let a WLED/serial problem interrupt the photobooth: log and
    swallow instead of raising out of a hookimpl (pibooth would otherwise
    treat it as an error and jump to the failsafe state)."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            LOGGER.exception("wled: unexpected error in %s", func.__name__)
    return wrapper


# --- pibooth hooks -----------------------------------------------------

name = "pibooth-wled-neopixel"


@pibooth.hookimpl
@_safe
def pibooth_configure(cfg):
    cfg.add_option('WLED', 'port', 'auto',
                    "Serial port of the ESP32/WLED controller "
                    "('auto' to detect it, or e.g. /dev/serial/by-id/usb-...-if00)")
    cfg.add_option('WLED', 'baudrate', 115200,
                    "Serial baud rate - must match WLED's Sync Interfaces > Serial setting")
    cfg.add_option('WLED', 'serial_timeout', 0.3,
                    "Serial read/write timeout in seconds")
    cfg.add_option('WLED', 'led_count', 0,
                    "Number of LEDs on the strip/ring (0 = auto-detect from WLED at startup)")
    cfg.add_option('WLED', 'segment_id', 0,
                    "WLED segment ID used for solid colours and the countdown's lit pixels")
    cfg.add_option('WLED', 'countdown_bg_segment_id', 1,
                    "WLED segment ID used for the countdown's background/off pixels "
                    "(must not be used for anything else on the device)")
    cfg.add_option('WLED', 'brightness', 180,
                    "Master brightness sent to WLED at startup (0-255)")
    cfg.add_option('WLED', 'transition_ms', 400,
                    "Crossfade duration for state changes, in milliseconds "
                    "(the capture stage is always applied instantly)")

    cfg.add_option('WLED', 'preview_countdown', True,
                    "Animate a live shrinking/growing bar on the LEDs during the preview delay")
    cfg.add_option('WLED', 'preview_countdown_color', "0,220,255",
                    "Colour of the remaining-time portion of the countdown bar "
                    "- R,G,B, or R,G,B,W for an RGBW strip")
    cfg.add_option('WLED', 'preview_countdown_off_color', "0,20,40",
                    "Colour of the elapsed-time portion of the countdown bar "
                    "- R,G,B, or R,G,B,W for an RGBW strip")
    cfg.add_option('WLED', 'preview_countdown_direction', "down",
                    "'down' shrinks the lit region as time runs out, 'up' grows it")
    cfg.add_option('WLED', 'preview_countdown_interval_ms', 120,
                    "How often (ms) to refresh the countdown bar over serial")

    for stage in STAGES:
        defaults = STAGE_DEFAULTS[stage]
        cfg.add_option('WLED', '{}_color'.format(stage), defaults['color'],
                        "Colour for the '{}' stage ({}) - R,G,B, or add a 4th value "
                        "(R,G,B,W) to also drive the white channel on an RGBW strip. "
                        "Always logical RGB(W): WLED's own per-output Color Order "
                        "setting handles the strip's actual physical wiring order, "
                        "this plugin never reorders channels itself.".format(stage, defaults['desc']))
        cfg.add_option('WLED', '{}_fx'.format(stage), defaults['fx'],
                        "Effect for the '{}' stage - either a WLED effect ID number "
                        "(paired with this stage's own _sx/_ix below), or one of the "
                        "built-in pattern names ({}), which also sets its own "
                        "recommended speed/intensity. -1 leaves the LEDs untouched "
                        "for this stage.".format(stage, ", ".join(sorted(PATTERNS))))
        cfg.add_option('WLED', '{}_sx'.format(stage), defaults['sx'],
                        "WLED effect speed 0-255 for the '{}' stage "
                        "(ignored when _fx above is a pattern name, not a number)".format(stage))
        cfg.add_option('WLED', '{}_ix'.format(stage), defaults['ix'],
                        "WLED effect intensity 0-255 for the '{}' stage "
                        "(ignored when _fx above is a pattern name, not a number)".format(stage))


@pibooth.hookimpl
@_safe
def pibooth_startup(cfg, app):
    _ctrl.configure(cfg)
    _ctrl.start()


@pibooth.hookimpl
@_safe
def pibooth_cleanup(app):
    _ctrl.cleanup()


@pibooth.hookimpl
@_safe
def state_wait_enter(app):
    _ctrl.apply_stage('wait')


@pibooth.hookimpl
@_safe
def state_choose_enter(app):
    _ctrl.apply_stage('choose')


@pibooth.hookimpl
@_safe
def state_chosen_enter(app):
    _ctrl.apply_stage('chosen')


@pibooth.hookimpl
@_safe
def state_preview_enter(cfg, app):
    _ctrl.apply_stage('preview')
    # Re-use pibooth's own preview delay so the LED countdown always stays
    # in sync with the on-screen one, without a second value to configure.
    duration = cfg.getint('WINDOW', 'preview_delay')
    _ctrl.start_countdown(duration)


@pibooth.hookimpl
@_safe
def state_preview_exit(app):
    _ctrl.stop_countdown()


@pibooth.hookimpl
@_safe
def state_capture_enter(app):
    # Applied instantly (no crossfade) so the fill light is at full
    # brightness before the first shot of the sequence is taken, and it
    # stays on for every capture in a multi-photo sequence.
    _ctrl.apply_stage('capture', instant=True)


@pibooth.hookimpl
@_safe
def state_processing_enter(app):
    _ctrl.apply_stage('processing')


@pibooth.hookimpl
@_safe
def state_print_enter(app):
    _ctrl.apply_stage('print')


@pibooth.hookimpl
@_safe
def state_finish_enter(app):
    _ctrl.apply_stage('finish')


@pibooth.hookimpl
@_safe
def state_failsafe_enter(app):
    _ctrl.stop_countdown()
    _ctrl.apply_stage('failsafe', instant=True)
