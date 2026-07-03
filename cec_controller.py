"""
CEC adapter controller — wraps a persistent cec-client.exe process.
All public methods are thread-safe.
"""

import logging
import subprocess
import threading
import time
from pathlib import Path

_CEC_EXE = Path(r"C:\Program Files (x86)\Pulse-Eight\USB-CEC Adapter\cec-client.exe")

_log = logging.getLogger("cec4htpc.cec")

try:
    import win32api
    import win32con
    import win32job
    _JOB_OK = True
except ImportError:
    _JOB_OK = False


class CECController:
    def __init__(self, adapter_hdmi_port: int = 1):
        # The physical HDMI port the adapter is actually wired into.
        # cec-client normally self-detects this via EDID from the port it's
        # plugged into, but on hardware where that detection is unreliable
        # (e.g. behind a switch/splitter that doesn't relay EDID/CEC physical
        # address correctly) it silently falls back to port 1, so the adapter
        # always reports itself as HDMI1 regardless of which port it's really
        # on. Passed to cec-client.exe as `-p` to override that detection —
        # see config.json's "adapter_hdmi_port".
        self._adapter_hdmi_port = adapter_hdmi_port
        self._proc  = None
        self._lock  = threading.Lock()
        # Guards the whole connect/disconnect/reconnect lifecycle. Without
        # this, two callers racing (e.g. Windows firing both
        # PBT_APMRESUMESUSPEND and PBT_APMRESUMEAUTOMATIC on the same
        # resume, or a resume overlapping the initial startup sequence)
        # could each spawn their own cec-client.exe at the same time. Only
        # one can ever open the adapter's COM port; the other fails with
        # "Access is denied" but we'd still have a 50/50 chance of
        # self._proc ending up pointing at the failed one — meaning every
        # CEC command sent afterward silently goes nowhere. RLock because
        # reconnect() calls disconnect() then connect() on the same thread.
        self._lifecycle_lock = threading.RLock()
        # Called (from the stdout-drain thread) with each raw line the
        # adapter prints — lets a UI (e.g. the CECRemote log box) mirror the
        # live cec-client.exe chatter instead of only seeing our own summaries.
        self.on_output = None

        # A Windows job object with KILL_ON_JOB_CLOSE ties the child
        # cec-client.exe's lifetime to ours: if this process crashes or is
        # force-killed (Task Manager, taskkill /F) without running our own
        # disconnect() cleanup, Windows tears the child down when it closes
        # our handles on exit. Without this, an orphaned cec-client.exe kept
        # the adapter's serial port open and no amount of restarting the
        # script could reclaim it — only a reboot freed the port.
        self._job = None
        if _JOB_OK:
            try:
                self._job = win32job.CreateJobObject(None, "")
                info = win32job.QueryInformationJobObject(
                    self._job, win32job.JobObjectExtendedLimitInformation
                )
                info["BasicLimitInformation"]["LimitFlags"] |= (
                    win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                )
                win32job.SetInformationJobObject(
                    self._job, win32job.JobObjectExtendedLimitInformation, info
                )
            except Exception:
                _log.exception("Failed to create job object for cec-client.exe cleanup")
                self._job = None

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def connect(self) -> tuple:
        with self._lifecycle_lock:
            if not _CEC_EXE.exists():
                _log.error("cec-client.exe not found at %s", _CEC_EXE)
                return False, f"cec-client.exe not found: {_CEC_EXE}"

            self._kill_stray_processes()

            try:
                _log.info("Launching cec-client.exe")
                self._proc = subprocess.Popen(
                    [str(_CEC_EXE), "-d", "1",   # interactive, errors-only log
                     "-p", str(self._adapter_hdmi_port)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self._assign_to_job(self._proc)
                threading.Thread(target=self._drain_stdout, daemon=True).start()
                time.sleep(3.5)
                if self._proc.poll() is not None:
                    _log.error(
                        "cec-client exited unexpectedly on startup (exit code %s)",
                        self._proc.returncode,
                    )
                    return False, "cec-client exited unexpectedly on startup"
                _log.info("Adapter connected (pid %s)", self._proc.pid)
                return True, "Adapter connected"
            except Exception as exc:
                _log.exception("Exception while launching cec-client.exe")
                return False, str(exc)

    def _assign_to_job(self, proc):
        if not self._job:
            return
        try:
            hProcess = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, proc.pid)
            win32job.AssignProcessToJobObject(self._job, hProcess)
        except Exception:
            _log.exception("Failed to assign cec-client.exe (pid %s) to job object", proc.pid)

    def _kill_stray_processes(self):
        """Best-effort sweep for orphaned cec-client.exe instances left by a
        prior crashed/force-killed run (or pre-dating the job-object fix
        above) that would otherwise hold the adapter's serial port open and
        make every connect() attempt fail until a reboot."""
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", "cec-client.exe"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                _log.warning(
                    "Killed stray cec-client.exe process(es) before connecting: %s",
                    result.stdout.strip(),
                )
        except Exception:
            _log.exception("Failed to sweep stray cec-client.exe processes")

    def _drain_stdout(self):
        try:
            for line in self._proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                _log.debug("cec-client: %s", line)
                if self.on_output:
                    try:
                        self.on_output(line)
                    except Exception:
                        _log.exception("on_output callback raised")
        except Exception:
            pass
        _log.info("cec-client.exe stdout stream ended (process exited)")

    def disconnect(self):
        with self._lifecycle_lock:
            if self._proc and self._proc.poll() is None:
                _log.info("Disconnecting from adapter (pid %s)", self._proc.pid)
                try:
                    self._proc.stdin.write("q\n")
                    self._proc.stdin.flush()
                    self._proc.wait(timeout=2)
                except Exception:
                    _log.warning("Clean quit failed, killing cec-client.exe process")
                    self._proc.kill()
            self._proc = None

    def reconnect(self) -> tuple:
        with self._lifecycle_lock:
            _log.info("Reconnecting to adapter")
            self.disconnect()
            time.sleep(1.0)
            return self.connect()

    def is_connected(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── internal ───────────────────────────────────────────────────────────────

    def _send(self, cmd: str, delay: float = 0.4) -> tuple:
        if not self.is_connected():
            _log.warning("Cannot send %r — adapter not connected", cmd)
            return False, "Not connected"
        try:
            with self._lock:
                _log.debug("Sending CEC command: %s", cmd)
                self._proc.stdin.write(cmd + "\n")
                self._proc.stdin.flush()
                time.sleep(delay)
            return True, cmd
        except BrokenPipeError:
            _log.error("Broken pipe sending %r — adapter connection lost", cmd)
            self._proc = None
            return False, "Lost connection to adapter"
        except Exception as exc:
            _log.exception("Unexpected error sending %r", cmd)
            return False, str(exc)

    # ── CEC commands ───────────────────────────────────────────────────────────

    def tv_on(self) -> tuple:
        """Send Image View On (CEC 0x04) directly to the TV — the explicit
        power-on request. Addressed to the TV's logical address, not a
        physical address, so unlike ActiveSource ("as") it can never send
        the TV to the wrong HDMI port."""
        return self._send("on 0", delay=1.5)

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

        NOTE: we intentionally never call power_on() (ActiveSource / 'as')
        here, nor anywhere in an automatic code path. 'as' broadcasts the
        adapter's own negotiated physical address rather than the port
        config.json says the PC is actually on — on this hardware that
        address always resolves to HDMI1. tv_on() wakes the display via a
        command addressed directly to the TV's logical address (no physical
        address involved, so it can't misdirect input); switch_input() claims
        the correct port via a raw ActiveSource frame carrying the configured
        physical address, never the adapter's own. Only manual "set HDMI
        port" controls (tray HDMI submenu, CECRemote's input picker) are
        exempt from this — they already work this same safe way.

        tv_on() is repeated on every retry rather than sent once up front,
        since a single attempt can be lost if the TV/bus isn't fully ready
        yet (e.g. right after a sleep/resume reconnect).

        Timeline: [tv_on → wait → switch_input → interval] × retries
        """
        _log.info(
            "Startup sequence: phys=%s retries=%d interval=%.1fs",
            phys_hex, retries, interval,
        )
        for i in range(retries):
            self.tv_on()
            time.sleep(3.0)    # TV needs time to wake before it accepts input commands
            self.switch_input(phys_hex)
            if i < retries - 1:
                time.sleep(interval)
        _log.info("Startup sequence complete")
