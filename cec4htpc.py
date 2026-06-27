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
import threading
import time
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from cec_controller import CECController
from power_monitor  import PowerMonitor
from volume_hook    import VolumeHook

# ── paths ──────────────────────────────────────────────────────────────────────

_HERE        = Path(__file__).parent
_CONFIG_PATH = _HERE / "config.json"

# ── defaults ───────────────────────────────────────────────────────────────────

_DEFAULTS = {
    # HDMI physical address: "1000"=HDMI1, "2000"=HDMI2, "3000"=HDMI3, "4000"=HDMI4
    "hdmi_input": "2000",

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
        self._cec   = CECController()
        self._icon  = None
        self._status_text = "Starting…"

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
        time.sleep(delay)

        ok, msg = self._cec.connect()
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

    # ── power event handlers ───────────────────────────────────────────────────

    def _on_sleep(self):
        if self._cfg.get("standby_on_sleep", True) and self._cec.is_connected():
            self._cec.standby()
        if not self._cfg.get("allow_tv_to_wake_pc", False):
            # Disconnect so cec-client is gone from the CEC bus while the PC
            # sleeps — reduces the chance the adapter fires a USB remote-wakeup
            # when the TV is turned on by hand.
            self._cec.disconnect()

    def _on_resume(self):
        if not self._cfg.get("wake_on_resume", True):
            return

        # USB-CEC adapter can take 5-15 s to re-enumerate after sleep.
        # Retry reconnect up to 5 times with 3 s gaps before giving up.
        time.sleep(5.0)
        for _ in range(5):
            if self._cec.is_connected():
                break
            ok, _ = self._cec.reconnect()
            if ok:
                break
            time.sleep(3.0)

        # On resume we only need 2 retries — no long fight against Apple TV,
        # just enough to assert our input after the TV finishes waking.
        self._cec.startup_sequence(
            phys_hex = self._cfg["hdmi_input"],
            retries  = 2,
            interval = 4.0,
        )

    def _on_shutdown(self):
        # Called synchronously from WM_ENDSESSION — must be quick
        if self._cfg.get("standby_on_shutdown", True) and self._cec.is_connected():
            self._cec.standby()

    # ── helpers ────────────────────────────────────────────────────────────────

    def _bg(self, fn):
        threading.Thread(target=fn, daemon=True, name="CEC-manual").start()

    def _reconnect(self):
        self._cec.disconnect()
        time.sleep(0.5)
        ok, msg = self._cec.connect()
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
        self._vol_hook.stop()
        self._power_mon.stop()
        self._cec.standby()          # optional: standby on quit
        time.sleep(0.7)
        self._cec.disconnect()
        if self._icon:
            self._icon.stop()


# ── entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = CEC4HTPC()
    app.run()
