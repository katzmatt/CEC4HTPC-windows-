"""
CEC adapter controller — wraps a persistent cec-client.exe process.
All public methods are thread-safe.
"""

import subprocess
import threading
import time
from pathlib import Path

_CEC_EXE = Path(r"C:\Program Files (x86)\Pulse-Eight\USB-CEC Adapter\cec-client.exe")


class CECController:
    def __init__(self):
        self._proc  = None
        self._lock  = threading.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def connect(self) -> tuple:
        if not _CEC_EXE.exists():
            return False, f"cec-client.exe not found: {_CEC_EXE}"
        try:
            self._proc = subprocess.Popen(
                [str(_CEC_EXE), "-d", "1"],   # interactive, errors-only log
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._drain_stdout, daemon=True).start()
            time.sleep(2.0)
            if self._proc.poll() is not None:
                return False, "cec-client exited unexpectedly on startup"
            return True, "Adapter connected"
        except Exception as exc:
            return False, str(exc)

    def _drain_stdout(self):
        try:
            for _ in self._proc.stdout:
                pass
        except Exception:
            pass

    def disconnect(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.write("q\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
        self._proc = None

    def reconnect(self) -> tuple:
        self.disconnect()
        time.sleep(1.0)
        return self.connect()

    def is_connected(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── internal ───────────────────────────────────────────────────────────────

    def _send(self, cmd: str, delay: float = 0.4) -> tuple:
        if not self.is_connected():
            return False, "Not connected"
        try:
            with self._lock:
                self._proc.stdin.write(cmd + "\n")
                self._proc.stdin.flush()
                time.sleep(delay)
            return True, cmd
        except BrokenPipeError:
            self._proc = None
            return False, "Lost connection to adapter"
        except Exception as exc:
            return False, str(exc)

    # ── CEC commands ───────────────────────────────────────────────────────────

    def tv_on(self) -> tuple:
        """Send Image View On (CEC 0x04) directly to the TV — the explicit
        power-on request.  Required to wake TV from standby; 'as' alone only
        switches input but won't always turn the display on."""
        return self._send("on 0", delay=1.5)

    def power_on(self) -> tuple:
        """Broadcast ActiveSource — claims our HDMI input on the TV."""
        return self._send("as", delay=0.6)

    def standby(self) -> tuple:
        """Send Standby to all CEC devices (broadcast)."""
        return self._send("standby 0", delay=0.6)

    def volume_up(self) -> tuple:
        return self._send("volup", delay=0.3)

    def volume_down(self) -> tuple:
        return self._send("voldown", delay=0.3)

    def mute_toggle(self) -> tuple:
        return self._send("mute", delay=0.3)

    def scan(self) -> tuple:
        """Scan the CEC bus; results are drained silently to the stdout reader."""
        return self._send("scan", delay=8.0)

    def switch_input(self, phys_hex: str) -> tuple:
        """
        Tell the TV to switch to the device at physical address phys_hex
        (e.g. '2000' = HDMI 2).  Sends a raw ActiveSource frame so the TV
        switches without us changing our own reported physical address.
        """
        addr = int(phys_hex, 16)
        hi   = (addr >> 8) & 0xFF
        lo   = addr & 0xFF
        return self._send(f"tx 1F:82:{hi:02X}:{lo:02X}", delay=0.5)

    # ── high-level sequences ───────────────────────────────────────────────────

    def startup_sequence(self, phys_hex: str, retries: int = 5,
                          interval: float = 8.0):
        """
        Wake TV and claim the HDMI input, retrying to beat aggressive devices.

        NOTE: we intentionally do NOT call power_on() (ActiveSource / 'as') here.
        'as' broadcasts the adapter's own physical address, which causes the TV
        to switch to whatever port the CEC adapter is on — not the PC's video
        port.  tv_on() wakes the display; switch_input() claims the correct port
        via a raw frame that doesn't expose the adapter's physical address.

        Timeline: tv_on → 3 s → [switch_input → interval] × retries
        """
        self.tv_on()
        time.sleep(3.0)    # TV needs time to wake before it accepts input commands
        for i in range(retries):
            self.switch_input(phys_hex)
            if i < retries - 1:
                time.sleep(interval)
