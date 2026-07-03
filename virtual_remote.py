"""
CEC Virtual Remote — forked from CECRemote for use inside CEC4HTPC.

When launched from the tray, a shared CECController is passed in so this
window uses the existing cec-client.exe connection rather than opening a
second one (which would fail).  All CEC commands go through the same lock-
protected subprocess that the tray app already owns.

Can also be run standalone (python virtual_remote.py) in which case it
manages its own connection identically to the original CECRemote.
"""

import json
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

from cec_controller import CECController

_CEC_EXE = Path(
    r"C:\Program Files (x86)\Pulse-Eight\USB-CEC Adapter\cec-client.exe"
)

_CONFIG_PATH = Path(__file__).parent / "config.json"


def _adapter_hdmi_port() -> int:
    """Read adapter_hdmi_port from config.json for standalone mode, so a
    manually-run CECRemote negotiates the same physical address as the tray
    app instead of falling back to the cec-client default of port 1."""
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f).get("adapter_hdmi_port", 1)
    except (OSError, json.JSONDecodeError):
        return 1


# ── GUI ────────────────────────────────────────────────────────────────────────

class VirtualRemote(tk.Tk):
    # ── palette ────────────────────────────────────────────────────────────────
    C_WIN_BG  = "#111827"
    C_BODY    = "#1f2937"
    C_DIV     = "#374151"
    C_TEXT    = "#f9fafb"
    C_DIM     = "#9ca3af"
    C_ACCENT  = "#f59e0b"

    C_PWR_ON  = "#991b1b";  C_PWR_ON_H = "#dc2626"
    C_STBY    = "#581c23";  C_STBY_H   = "#7f1d1d"
    C_VOL     = "#1e3a8a";  C_VOL_H    = "#1d4ed8"
    C_MUTE    = "#78350f";  C_MUTE_H   = "#92400e"
    C_HDMI    = "#14532d";  C_HDMI_H   = "#15803d"
    C_SCAN    = "#312e81";  C_SCAN_H   = "#4338ca"

    C_LOG_BG  = "#0f172a"
    C_LOG_OK  = "#86efac"
    C_LOG_ERR = "#fca5a5"
    C_LOG_RAW = "#64748b"
    C_DOT_OK  = "#22c55e"
    C_DOT_ERR = "#ef4444"

    def __init__(self, cec: CECController = None):
        super().__init__()

        # Shared mode: caller owns the controller and its lifecycle.
        # Standalone mode: we own it and must connect/disconnect ourselves.
        if cec is not None:
            self._cec    = cec
            self._shared = True
        else:
            self._cec    = CECController(adapter_hdmi_port=_adapter_hdmi_port())
            self._shared = False

        self.title("CEC Virtual Remote")
        self.configure(bg=self.C_WIN_BG)
        self.resizable(False, False)
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Mirror the adapter's raw cec-client.exe output (bus traffic, errors,
        # libcec warnings) into the log box for troubleshooting, not just our
        # own terse command acks.
        self._cec.on_output = self._append_raw_log

        if self._shared:
            # Connection already open — just reflect current state.
            self.after(
                200,
                lambda: self._on_connect(
                    self._cec.is_connected(),
                    "Using shared CEC connection" if self._cec.is_connected()
                    else "Not connected — use Reconnect",
                ),
            )
        else:
            self.after(300, self._detect_then_connect)

    # ── layout ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        wrap = tk.Frame(self, bg=self.C_WIN_BG, padx=14, pady=14)
        wrap.pack()
        body = tk.Frame(wrap, bg=self.C_BODY, padx=20, pady=18)
        body.pack()

        tk.Label(body, text="  CEC  REMOTE  ", bg=self.C_BODY, fg=self.C_ACCENT,
                  font=("Helvetica", 16, "bold")).pack(pady=(0, 6))

        sf = tk.Frame(body, bg=self.C_BODY)
        sf.pack(fill="x", pady=(0, 10))
        self._dot = tk.Label(sf, text="●", bg=self.C_BODY, fg=self.C_DOT_ERR,
                               font=("Helvetica", 12))
        self._dot.pack(side="left")
        self._status = tk.Label(sf, text="Starting…", bg=self.C_BODY,
                                  fg=self.C_DIM, font=("Helvetica", 9))
        self._status.pack(side="left", padx=5)
        self._conn_btn = self._btn(sf, "Reconnect", self.C_SCAN, self.C_SCAN_H,
                                    self._reconnect, w=10, h=1,
                                    font_=("Helvetica", 8))
        self._conn_btn.pack(side="right")

        # POWER
        self._hr(body); self._lbl(body, "POWER")
        pf = tk.Frame(body, bg=self.C_BODY); pf.pack(pady=5)
        # tv_on() addresses the TV's logical address directly and carries no
        # physical-address payload, so — unlike ActiveSource ("as") — it can
        # never send the TV to the wrong HDMI port. Use the HDMI INPUT picker
        # below to explicitly claim a port.
        self._btn(pf, "⏻   POWER ON", self.C_PWR_ON, self.C_PWR_ON_H,
                   lambda: self._fire(self._cec.tv_on)).pack(pady=3)
        self._btn(pf, "⏼   STANDBY",  self.C_STBY,   self.C_STBY_H,
                   lambda: self._fire(self._cec.standby)).pack(pady=3)

        # VOLUME
        self._hr(body); self._lbl(body, "VOLUME")
        vf = tk.Frame(body, bg=self.C_BODY); vf.pack(pady=5)
        self._btn(vf, "▲  VOL +", self.C_VOL,  self.C_VOL_H,
                   lambda: self._fire(self._cec.volume_up),  w=13).grid(
                       row=0, column=0, padx=4, pady=3)
        self._btn(vf, "🔇  MUTE",  self.C_MUTE, self.C_MUTE_H,
                   lambda: self._fire(self._cec.mute_toggle), w=13).grid(
                       row=0, column=1, padx=4, pady=3)
        self._btn(vf, "▼  VOL -", self.C_VOL,  self.C_VOL_H,
                   lambda: self._fire(self._cec.volume_down), w=13).grid(
                       row=1, column=0, padx=4, pady=3)

        # HDMI INPUT
        self._hr(body); self._lbl(body, "HDMI INPUT")
        hf = tk.Frame(body, bg=self.C_BODY); hf.pack(pady=5)

        _INPUTS = [
            ("HDMI 1  (1.0.0.0)", "1000"),
            ("HDMI 2  (2.0.0.0)", "2000"),
            ("HDMI 3  (3.0.0.0)", "3000"),
            ("HDMI 4  (4.0.0.0)", "4000"),
        ]
        self._hdmi_var = tk.StringVar(value=_INPUTS[0][0])
        self._hdmi_map = {label: addr for label, addr in _INPUTS}

        om = tk.OptionMenu(hf, self._hdmi_var, *[lbl for lbl, _ in _INPUTS])
        om.config(bg=self.C_HDMI, fg=self.C_TEXT, activebackground=self.C_HDMI_H,
                  activeforeground=self.C_TEXT, highlightthickness=0,
                  font=("Helvetica", 10), relief="flat", bd=0, width=22)
        om["menu"].config(bg=self.C_HDMI, fg=self.C_TEXT,
                          activebackground=self.C_HDMI_H, activeforeground=self.C_TEXT)
        om.pack(pady=(0, 6))

        self._btn(hf, "▶  Switch to this input", self.C_HDMI, self.C_HDMI_H,
                  self._do_hdmi_switch, w=28).pack()

        # Scan
        self._hr(body)
        self._btn(body, "⟳  Scan Devices", self.C_SCAN, self.C_SCAN_H,
                   lambda: self._fire(self._cec.scan), w=30).pack(pady=(4, 8))

        # Log
        self._lbl(body, "LOG")
        lf = tk.Frame(body, bg=self.C_LOG_BG, padx=5, pady=5)
        lf.pack(fill="x")
        self._log = tk.Text(lf, bg=self.C_LOG_BG, fg=self.C_LOG_OK,
                             font=("Courier New", 8), height=5, width=38,
                             state="disabled", wrap="word", bd=0)
        self._log.tag_config("ok",  foreground=self.C_LOG_OK)
        self._log.tag_config("err", foreground=self.C_LOG_ERR)
        self._log.tag_config("raw", foreground=self.C_LOG_RAW)
        self._log.pack()

    # ── helpers ────────────────────────────────────────────────────────────────

    def _hr(self, p):
        tk.Frame(p, bg=self.C_DIV, height=1).pack(fill="x", pady=7)

    def _lbl(self, p, t):
        tk.Label(p, text=t, bg=self.C_BODY, fg=self.C_DIM,
                  font=("Helvetica", 8, "bold")).pack()

    def _btn(self, parent, text, bg, hbg, cmd,
              w=28, h=2, font_=("Helvetica", 10, "bold")):
        b = tk.Button(parent, text=text, bg=bg, fg=self.C_TEXT,
                       activebackground=hbg, activeforeground=self.C_TEXT,
                       relief="flat", font=font_, width=w, height=h,
                       cursor="hand2", command=cmd, bd=0,
                       highlightthickness=0)
        b.bind("<Enter>", lambda _, btn=b, c=hbg: btn.config(bg=c))
        b.bind("<Leave>", lambda _, btn=b, c=bg:  btn.config(bg=c))
        return b

    # ── connect / reconnect ────────────────────────────────────────────────────

    def _detect_then_connect(self):
        """Standalone mode only — check adapter then connect."""
        self._set_status("Checking adapter…", ok=False)

        def worker():
            if not _CEC_EXE.exists():
                msg = f"cec-client.exe not found"
                self.after(0, lambda: (self._set_status(msg, ok=False),
                                        self._log_line(msg, ok=False)))
                return
            self._connect_worker()

        threading.Thread(target=worker, daemon=True).start()

    def _reconnect(self):
        self._set_status("Reconnecting…", ok=False)

        def worker():
            self._cec.disconnect()
            time.sleep(0.5)
            self._connect_worker()

        threading.Thread(target=worker, daemon=True).start()

    def _connect_worker(self):
        self.after(0, lambda: self._set_status("Connecting…", ok=False))
        ok, msg = self._cec.connect()
        self.after(0, lambda: self._on_connect(ok, msg))

    def _on_connect(self, ok, msg):
        self._set_status("Ready" if ok else "Disconnected", ok=ok)
        self._log_line(msg, ok)

    def _set_status(self, text, ok):
        self._dot.config(fg=self.C_DOT_OK if ok else self.C_DOT_ERR)
        self._status.config(text=text, fg=self.C_TEXT if ok else self.C_DIM)

    # ── command dispatch ───────────────────────────────────────────────────────

    def _fire(self, fn):
        def worker():
            ok, msg = fn()
            self.after(0, lambda: self._log_line(msg, ok))
        threading.Thread(target=worker, daemon=True).start()

    def _do_hdmi_switch(self):
        phys = self._hdmi_map[self._hdmi_var.get()]
        self._fire(lambda: self._cec.switch_input(phys))

    def _log_line(self, msg, ok=True):
        tag = "ok" if ok else "err"
        prefix = ("✓ " if ok else "✗ ")
        self._append_log_line(prefix + msg, tag)

    def _append_raw_log(self, line):
        """Called from the cec-client.exe stdout-drain thread — marshal onto
        the Tk main thread before touching any widget."""
        self.after(0, lambda: self._append_log_line(line, "raw"))

    def _append_log_line(self, text, tag):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.config(state="normal")
        self._log.insert("end", f"{ts}  {text}\n", tag)
        self._log.see("end")
        end_row = int(self._log.index("end-1c").split(".")[0])
        if end_row > 200:
            self._log.delete("1.0", f"{end_row - 200}.0")
        self._log.config(state="disabled")

    def _on_close(self):
        if self._cec.on_output is self._append_raw_log:
            self._cec.on_output = None
        if not self._shared:
            self._cec.disconnect()
        self.destroy()


# ── entry point (standalone) ───────────────────────────────────────────────────

if __name__ == "__main__":
    app = VirtualRemote()
    app.mainloop()
