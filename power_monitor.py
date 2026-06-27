"""
Power event monitor — listens for Windows sleep/resume and shutdown messages
via a hidden Win32 top-level window on a dedicated background thread.

Callbacks are called from that thread; keep them short or hand off to threads.
"""

import threading

try:
    import win32api
    import win32con
    import win32gui
    _WIN32_OK = True
except ImportError:
    _WIN32_OK = False

# Windows message constants
_WM_QUERYENDSESSION = 0x0011
_WM_ENDSESSION      = 0x0016
_WM_POWERBROADCAST  = 0x0218
_PBT_APMSUSPEND            = 0x0004   # system is suspending
_PBT_APMRESUMESUSPEND      = 0x0007   # resumed after user-initiated suspend
_PBT_APMRESUMEAUTOMATIC    = 0x0012   # resumed (may not have user at keyboard)

_CLASS_NAME = "CEC4HTPCPowerMonitor"


class PowerMonitor:
    """
    Creates a hidden Win32 window and pumps its message queue on a daemon
    thread.  Invokes on_sleep / on_resume / on_shutdown at the appropriate
    system events.
    """

    def __init__(self, on_sleep=None, on_resume=None, on_shutdown=None):
        self._on_sleep    = on_sleep    or (lambda: None)
        self._on_resume   = on_resume   or (lambda: None)
        self._on_shutdown = on_shutdown or (lambda: None)
        self._hwnd  = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="PowerMonitor", daemon=True
        )

    def start(self):
        if not _WIN32_OK:
            return   # pywin32 not installed — skip silently
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def stop(self):
        if self._hwnd:
            try:
                win32gui.PostMessage(self._hwnd, win32con.WM_QUIT, 0, 0)
            except Exception:
                pass

    # ── window thread ──────────────────────────────────────────────────────────

    def _run(self):
        hInst = win32api.GetModuleHandle(None)

        wc             = win32gui.WNDCLASS()
        wc.hInstance   = hInst
        wc.lpszClassName = _CLASS_NAME
        wc.lpfnWndProc = self._wndproc

        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass   # already registered from a previous run in this process

        # Invisible top-level popup — WM_QUERYENDSESSION/WM_ENDSESSION require
        # a top-level window (not a message-only HWND_MESSAGE window).
        self._hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE,
            _CLASS_NAME,
            "CEC4HTPC",
            win32con.WS_POPUP,
            -1, -1, 1, 1,   # 1×1 pixel, off-screen
            0, 0, hInst, None,
        )

        self._ready.set()
        win32gui.PumpMessages()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == _WM_POWERBROADCAST:
            if wparam == _PBT_APMSUSPEND:
                threading.Thread(
                    target=self._on_sleep, daemon=True, name="CEC-sleep"
                ).start()
            elif wparam in (_PBT_APMRESUMESUSPEND, _PBT_APMRESUMEAUTOMATIC):
                threading.Thread(
                    target=self._on_resume, daemon=True, name="CEC-resume"
                ).start()
            return True

        elif msg == _WM_QUERYENDSESSION:
            # Signal that we consent to shutdown; do actual work in WM_ENDSESSION
            return True

        elif msg == _WM_ENDSESSION:
            if wparam:
                # Synchronous — Windows gives us ~5 s before force-killing;
                # the CEC standby command takes ~0.6 s, well within that window.
                self._on_shutdown()
            return 0

        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)
