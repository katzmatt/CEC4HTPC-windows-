"""
CEC4HTPC — HTPC CEC Automation for Windows
System-tray app that:
  • Powers the TV on at startup and claims a configurable HDMI input (retried
    to beat aggressive devices like Apple TV).
  • Standbys the TV on system shutdown and sleep, wakes it on resume.
  • Locks Windows master volume at 100 % and routes volume-key presses to
    the TV/soundbar over CEC instead.
  • Provides a tray icon with quick controls and a shortcut to CECRemote.

Requirements:  pip install pywin32 pystray Pillow keyboard pycaw comtypes
Run as admin (or via Task Scheduler /rl highest) for reliable key suppression.
"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from cec_controller import CECController
from logging_setup  import setup_logging
from power_monitor  import PowerMonitor
from volume_hook    import VolumeHook

try:
    import win32api
    import win32event
    import winerror
    _SINGLETON_OK = True
except ImportError:
    _SINGLETON_OK = False

_log = logging.getLogger("cec4htpc.app")

_SINGLETON_MUTEX_NAME = "Global\\CEC4HTPC_SingleInstance"

# ── paths ──────────────────────────────────────────────────────────────────────

_HERE        = Path(__file__).parent
_CONFIG_PATH = _HERE / "config.json"

# ── defaults ───────────────────────────────────────────────────────────────────

_DEFAULTS = {
    # HDMI physical address: "1000"=HDMI1, "2000"=HDMI2, "3000"=HDMI3, "4000"=HDMI4
    "hdmi_input": "2000",

    # The physical TV HDMI port the adapter itself is plugged into (1-4).
    # cec-client normally auto-detects this via EDID, but some setups (e.g.
    # an HDMI switch/splitter between the adapter and the TV) don't relay
    # that correctly, so the adapter always negotiates as HDMI1 regardless
    # of which port it's really wired to. Set this to override — e.g. if the
    # adapter is physically on HDMI3, set this to 3 even though it currently
    # shows up as HDMI1.
    "adapter_hdmi_port": 1,

    # Seconds to wait after launch before doing anything (lets the desktop settle)
    "startup_delay_seconds": 5,

    # How many times to assert our HDMI input on startup (to beat Apple TV etc.)
    "startup_retry_count": 5,

    # Seconds between each retry assertion
    "startup_retry_interval_seconds": 8,

    # Feature toggles
    "power_on_at_startup":   True,
    "standby_on_shutdown":   True,
    "standby_on_sleep":      True,
    "wake_on_resume":        True,
    "lock_volume":           True,

    # When false (default), CEC is disconnected before sleep so the adapter
    # sits idle on the CEC bus and is less likely to send a USB remote-wakeup
    # signal when the TV is turned on.  Set true if you want the TV power
    # button to wake the PC.  Note: Windows Device Manager > USB-CEC Adapter >
    # Power Management > "Allow this device to wake the computer" is the
    # hardware-level control; this toggle is the software-layer companion.
    "allow_tv_to_wake_pc":   False,
}

_HDMI_INPUTS = [
    ("HDMI 1", "1000"),
    ("HDMI 2", "2000"),
    ("HDMI 3", "3000"),
    ("HDMI 4", "4000"),
]


# ── config ─────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return {**_DEFAULTS, **json.load(f)}
    cfg = dict(_DEFAULTS)
    _save_config(cfg)
    return cfg


def _save_config(cfg: dict):
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ── tray icon image ────────────────────────────────────────────────────────────

def _make_icon(size: int = 64) -> Image.Image:
    """Draw a simple TV silhouette in the CECRemote green palette."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    s   = size
    # Outer TV body
    d.rectangle([s*.06, s*.10, s*.94, s*.72], fill=(31, 41, 55),
                outline=(22, 163, 74), width=max(2, s//24))
    # Screen
    d.rectangle([s*.12, s*.16, s*.88, s*.66], fill=(21, 128, 61))
    # Stand pole
    d.rectangle([s*.43, s*.72, s*.57, s*.86], fill=(55, 65, 81))
    # Stand base
    d.rectangle([s*.25, s*.86, s*.75, s*.93], fill=(55, 65, 81))
    return img


# ── main application ───────────────────────────────────────────────────────────

class CEC4HTPC:
    def __init__(self):
        self._cfg   = _load_config()
        self._cec   = CECController(
            adapter_hdmi_port=self._cfg.get("adapter_hdmi_port", 1)
        )
        self._icon  = None
        self._status_text = "Starting…"
        # Windows fires both PBT_APMRESUMESUSPEND and PBT_APMRESUMEAUTOMATIC
        # for a single resume (confirmed in the logs, ~1s apart), each
        # spawning its own _on_resume thread. Without this guard both run
        # the full reconnect sequence concurrently and race to spawn a
        # cec-client.exe each — only one can open the adapter's COM port,
        # but self._cec could end up tracking whichever one lost that race,
        # silently swallowing every CEC command sent afterward.
        self._resume_lock = threading.Lock()

        self._power_mon = PowerMonitor(
            on_sleep    = self._on_sleep,
            on_resume   = self._on_resume,
            on_shutdown = self._on_shutdown,
        )
        self._vol_hook = VolumeHook(
            on_vol_up   = lambda: self._cec.volume_up(),
            on_vol_down = lambda: self._cec.volume_down(),
            on_mute     = lambda: self._cec.mute_toggle(),
        )

    # ── entry point ────────────────────────────────────────────────────────────

    def run(self):
        self._power_mon.start()

        # Startup sequence runs in background while tray icon is building
        threading.Thread(
            target=self._startup_sequence,
            daemon=True,
            name="CEC-startup",
        ).start()

        self._icon = pystray.Icon(
            name  = "CEC4HTPC",
            icon  = _make_icon(),
            title = "CEC4HTPC",
            menu  = self._build_menu(),
        )
        self._icon.run()   # blocks until quit

    # ── tray menu ──────────────────────────────────────────────────────────────

    def _build_menu(self):
        def _input_item(label, addr):
            return pystray.MenuItem(
                label,
                action  = lambda icon, item: self._set_hdmi(addr),
                checked = lambda item: self._cfg["hdmi_input"] == addr,
                radio   = True,
            )

        return pystray.Menu(
            pystray.MenuItem("CEC4HTPC", None, enabled=False),
            pystray.Menu.SEPARATOR,
            # ── TV power ──
            pystray.MenuItem(
                "Power On TV",
                lambda *_: self._bg(lambda: self._cec.startup_sequence(
                    self._cfg["hdmi_input"], retries=1, interval=0,
                )),
            ),
            pystray.MenuItem(
                "Standby TV",
                lambda *_: self._bg(self._cec.standby),
            ),
            pystray.Menu.SEPARATOR,
            # ── HDMI input submenu ──
            pystray.MenuItem(
                "HDMI Input",
                pystray.Menu(*[_input_item(l, a) for l, a in _HDMI_INPUTS]),
            ),
            pystray.MenuItem(
                "Switch to Selected Input Now",
                lambda *_: self._bg(
                    lambda: self._cec.switch_input(self._cfg["hdmi_input"])
                ),
            ),
            pystray.Menu.SEPARATOR,
            # ── tools ──
            pystray.MenuItem("Open CECRemote",   self._open_cecremote),
            pystray.MenuItem("Reconnect Adapter",
                             lambda *_: self._bg(self._reconnect)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    # ── startup sequence ───────────────────────────────────────────────────────

    def _startup_sequence(self):
        delay = self._cfg.get("startup_delay_seconds", 5)
        _log.info("Startup sequence: waiting %ss for desktop to settle", delay)
        time.sleep(delay)

        ok, msg = self._cec.connect()
        _log.info("Initial connect: ok=%s msg=%s", ok, msg)
        self._status_text = msg

        if not ok:
            self._update_tray_title(f"CEC4HTPC — {msg}")
            return

        if self._cfg.get("lock_volume", True):
            self._vol_hook.start()

        if self._cfg.get("power_on_at_startup", True):
            self._update_tray_title("CEC4HTPC — Powering on TV…")
            self._cec.startup_sequence(
                phys_hex = self._cfg["hdmi_input"],
                retries  = self._cfg.get("startup_retry_count", 5),
                interval = self._cfg.get("startup_retry_interval_seconds", 8),
            )

        self._update_tray_title("CEC4HTPC — Ready")
        _log.info("Startup sequence complete")

    # ── power event handlers ───────────────────────────────────────────────────

    def _on_sleep(self):
        _log.info("Sleep event: standby_on_sleep=%s allow_tv_to_wake_pc=%s connected=%s",
                   self._cfg.get("standby_on_sleep", True),
                   self._cfg.get("allow_tv_to_wake_pc", False),
                   self._cec.is_connected())
        if self._cfg.get("standby_on_sleep", True) and self._cec.is_connected():
            self._cec.standby()
        if not self._cfg.get("allow_tv_to_wake_pc", False):
            # Disconnect so cec-client is gone from the CEC bus while the PC
            # sleeps — reduces the chance the adapter fires a USB remote-wakeup
            # when the TV is turned on by hand.
            self._cec.disconnect()
        else:
            _log.info("Leaving cec-client running across sleep (allow_tv_to_wake_pc=true)")

    def _on_resume(self):
        # Windows reliably fires both PBT_APMRESUMESUSPEND and
        # PBT_APMRESUMEAUTOMATIC for one resume; PowerMonitor spawns a
        # thread per message. Drop the second one outright rather than
        # letting two reconnect sequences race for the adapter's COM port.
        if not self._resume_lock.acquire(blocking=False):
            _log.info("Resume event ignored — a resume sequence is already running "
                      "(this is the duplicate Windows power message, not a bug)")
            return
        try:
            self._handle_resume()
        finally:
            self._resume_lock.release()

    def _handle_resume(self):
        _log.info("Resume event: wake_on_resume=%s", self._cfg.get("wake_on_resume", True))
        if not self._cfg.get("wake_on_resume", True):
            return

        # The USB-CEC adapter is a physical device Windows can power-cycle or
        # re-enumerate across sleep even when our cec-client.exe process
        # survives (poll() still reports "running"). Trusting is_connected()
        # here used to skip reconnecting whenever the pre-sleep process
        # looked alive, leaving a dead connection that no amount of
        # restarting the script fixed — only a reboot re-enumerated the
        # adapter. Always force a fresh disconnect+reconnect on resume
        # instead of reusing pre-sleep state.
        time.sleep(5.0)

        ok, msg = False, ""
        for attempt in range(1, 6):
            _log.info("Resume reconnect attempt %d/5", attempt)
            ok, msg = self._cec.reconnect()
            _log.info("Resume reconnect attempt %d result: ok=%s msg=%s", attempt, ok, msg)
            if ok:
                break
            time.sleep(3.0)

        if not ok:
            _log.error("Resume: failed to reconnect to adapter after 5 attempts (%s)", msg)
            self._update_tray_title("CEC4HTPC — Adapter reconnect failed")
            return

        # Extra buffer: cec-client reports "connected" as soon as its process
        # starts, but may not have finished negotiating the CEC bus yet.
        time.sleep(2.0)

        # NOTE: we deliberately never use power_on() ("as") here. That sends
        # ActiveSource with the adapter's own negotiated physical address,
        # which on this hardware always resolves to HDMI1 — not whatever
        # port config.json says the PC is actually on. tv_on() ("on 0") is
        # addressed directly to the TV's logical address and carries no
        # physical-address payload, so it can't misdirect the input; it's
        # repeated on every retry (not just once) since a single attempt
        # right after reconnect isn't reliably landing before the TV/bus is
        # fully ready.
        _log.info("Resume: reasserting HDMI input %s", self._cfg["hdmi_input"])
        self._cec.startup_sequence(
            phys_hex = self._cfg["hdmi_input"],
            retries  = 5,
            interval = 5.0,
        )
        self._update_tray_title("CEC4HTPC — Ready")
        _log.info("Resume sequence complete")

    def _on_shutdown(self):
        # Called synchronously from WM_ENDSESSION — must be quick
        _log.info("Shutdown event: standby_on_shutdown=%s connected=%s",
                   self._cfg.get("standby_on_shutdown", True), self._cec.is_connected())
        if self._cfg.get("standby_on_shutdown", True) and self._cec.is_connected():
            self._cec.standby()

    # ── helpers ────────────────────────────────────────────────────────────────

    def _bg(self, fn):
        threading.Thread(target=fn, daemon=True, name="CEC-manual").start()

    def _reconnect(self):
        _log.info("Manual reconnect requested from tray menu")
        self._cec.disconnect()
        time.sleep(0.5)
        ok, msg = self._cec.connect()
        _log.info("Manual reconnect result: ok=%s msg=%s", ok, msg)
        self._update_tray_title(f"CEC4HTPC — {'Ready' if ok else msg}")

    def _set_hdmi(self, addr: str):
        self._cfg["hdmi_input"] = addr
        _save_config(self._cfg)
        self._bg(lambda: self._cec.switch_input(addr))
        if self._icon:
            self._icon.update_menu()

    def _update_tray_title(self, text: str):
        if self._icon:
            self._icon.title = text

    def _open_cecremote(self, *_):
        # Guard: don't open a second window if one is already running.
        if hasattr(self, "_remote_thread") and self._remote_thread.is_alive():
            return

        def _run():
            from virtual_remote import VirtualRemote
            remote = VirtualRemote(cec=self._cec)
            remote.mainloop()

        self._remote_thread = threading.Thread(
            target=_run, daemon=True, name="CECRemote"
        )
        self._remote_thread.start()

    def _quit(self, *_):
        # Quitting CEC4HTPC (e.g. to restart it) should not standby the TV —
        # the user is still sitting in front of it.
        _log.info("Quit requested from tray menu")
        self._vol_hook.stop()
        self._power_mon.stop()
        self._cec.disconnect()
        if self._icon:
            self._icon.stop()


# ── single instance guard ────────────────────────────────────────────────────

def _acquire_single_instance_lock():
    """Prevent a second CEC4HTPC instance from launching alongside one that's
    already running. Two instances fighting over the same cec-client.exe /
    serial port is another way the adapter connection ends up wedged until
    the offending process is found and killed manually."""
    if not _SINGLETON_OK:
        return None, False
    mutex = win32event.CreateMutex(None, False, _SINGLETON_MUTEX_NAME)
    already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    return mutex, already_running


# ── entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_logging()
    _log.info("=" * 60)
    _log.info("CEC4HTPC starting (pid %s)", os.getpid())

    _mutex, _already_running = _acquire_single_instance_lock()
    if _already_running:
        _log.warning("Another CEC4HTPC instance is already running — exiting.")
        sys.exit(0)

    try:
        app = CEC4HTPC()
        app.run()
    except Exception:
        _log.exception("CEC4HTPC crashed")
        raise
    finally:
        _log.info("CEC4HTPC exiting")
