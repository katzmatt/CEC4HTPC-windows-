"""
Volume hook — intercepts volume keys via RegisterHotKey and pins Windows master
volume at 100 %, routing presses to CEC instead.

RegisterHotKey is preferred over WH_KEYBOARD_LL because multimedia volume keys
on many laptops and keyboards arrive as WM_APPCOMMAND messages to the foreground
window, bypassing keyboard hooks entirely.  RegisterHotKey catches both paths.

The trade-off: RegisterHotKey cannot suppress the key before Windows processes
it.  We compensate with an immediate volume pin (50 ms after each press) so
the Windows volume blip is imperceptible.

Requires: pywin32, pycaw, comtypes
"""

import threading
import time

try:
    import win32api
    import win32con
    import win32gui
    _WIN32_OK = True
except ImportError:
    _WIN32_OK = False

try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    _PYCAW_OK = True
except ImportError:
    _PYCAW_OK = False

# Virtual key codes for multimedia volume keys
_VK_VOLUME_MUTE = 0xAD
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_UP   = 0xAF

# MOD_NOREPEAT suppresses auto-repeated WM_HOTKEY while the key is held
_MOD_NOREPEAT = 0x4000

_WM_HOTKEY = 0x0312

_ID_MUTE = 200
_ID_DOWN = 201
_ID_UP   = 202

_CLASS = "CEC4HTPCVolHook"


class VolumeHook:
    def __init__(self, on_vol_up, on_vol_down, on_mute):
        self._on_vol_up   = on_vol_up
        self._on_vol_down = on_vol_down
        self._on_mute     = on_mute
        self._vol_iface   = None
        self._active      = False
        self._hwnd        = None
        self._ready       = threading.Event()

    # ── public ─────────────────────────────────────────────────────────────────

    def start(self):
        self._active = True
        self._init_volume()
        self._start_watchdog()
        if _WIN32_OK:
            self._start_hotkey_window()

    def stop(self):
        self._active = False
        if self._hwnd and _WIN32_OK:
            try:
                win32gui.PostMessage(self._hwnd, win32con.WM_QUIT, 0, 0)
            except Exception:
                pass

    # ── Windows volume ─────────────────────────────────────────────────────────

    def _init_volume(self):
        if not _PYCAW_OK:
            return
        try:
            devices = AudioUtilities.GetSpeakers()
            iface   = devices.Activate(
                IAudioEndpointVolume._iid_, CLSCTX_ALL, None
            )
            self._vol_iface = cast(iface, POINTER(IAudioEndpointVolume))
            self._pin_volume()
        except Exception:
            self._vol_iface = None

    def _pin_volume(self):
        if self._vol_iface:
            try:
                self._vol_iface.SetMasterVolumeLevelScalar(1.0, None)
            except Exception:
                pass

    def _start_watchdog(self):
        def _loop():
            while self._active:
                self._pin_volume()
                time.sleep(10.0)
        threading.Thread(target=_loop, daemon=True, name="VolWatchdog").start()

    # ── RegisterHotKey window ──────────────────────────────────────────────────

    def _start_hotkey_window(self):
        def _run():
            hInst = win32api.GetModuleHandle(None)
            wc             = win32gui.WNDCLASS()
            wc.hInstance   = hInst
            wc.lpszClassName = _CLASS
            wc.lpfnWndProc = self._wndproc
            try:
                win32gui.RegisterClass(wc)
            except Exception:
                pass   # already registered

            self._hwnd = win32gui.CreateWindowEx(
                win32con.WS_EX_TOOLWINDOW,
                _CLASS, "",
                win32con.WS_POPUP,
                -1, -1, 1, 1,
                0, 0, hInst, None,
            )
            win32api.RegisterHotKey(self._hwnd, _ID_MUTE, _MOD_NOREPEAT, _VK_VOLUME_MUTE)
            win32api.RegisterHotKey(self._hwnd, _ID_DOWN, _MOD_NOREPEAT, _VK_VOLUME_DOWN)
            win32api.RegisterHotKey(self._hwnd, _ID_UP,   _MOD_NOREPEAT, _VK_VOLUME_UP)
            self._ready.set()
            win32gui.PumpMessages()

        threading.Thread(target=_run, daemon=True, name="VolHotkeyWnd").start()
        self._ready.wait(timeout=3.0)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == _WM_HOTKEY:
            if wparam == _ID_UP:
                self._dispatch(self._on_vol_up)
            elif wparam == _ID_DOWN:
                self._dispatch(self._on_vol_down)
            elif wparam == _ID_MUTE:
                self._dispatch(self._on_mute)
            # Pin volume quickly — RegisterHotKey can't suppress the key so
            # Windows audio briefly processes it; reset before user notices.
            threading.Thread(
                target=lambda: (time.sleep(0.05), self._pin_volume()),
                daemon=True, name="VolPin",
            ).start()
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _dispatch(self, fn):
        threading.Thread(target=fn, daemon=True, name="CEC-vol").start()
