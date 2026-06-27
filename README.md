# CEC4HTPC (Windows)

Headless CEC automation for Windows HTPCs. Runs as a system-tray app that starts with Windows and automatically controls your TV over HDMI-CEC using the [Pulse-Eight USB-CEC Adapter](https://www.pulse-eight.com/p/104/usb-hdmi-cec-adapter).

---

## Features

| Feature | Description |
|---|---|
| **Startup** | Powers on the TV and switches to your configured HDMI input at login. Retries repeatedly to reclaim the input from aggressive devices (e.g. Apple TV) that also broadcast `ActiveSource` on wake. |
| **Shutdown** | Standbys the TV when Windows shuts down. |
| **Sleep / Resume** | Standbys the TV on sleep; wakes it and reclaims the input on resume. |
| **Volume Lock** | Pins Windows master volume at 100%. Volume Up/Down/Mute key presses are intercepted and sent as CEC commands to the TV or soundbar instead. |
| **System Tray** | Right-click menu with quick power controls, per-port HDMI input selection, adapter reconnect, and a shortcut to the bundled CEC Virtual Remote for manual control. |

---

## Requirements

- Windows 10 or 11
- Python 3.10+
- [Pulse-Eight USB-CEC Adapter](https://www.pulse-eight.com/p/104/usb-hdmi-cec-adapter) with drivers installed
  - Default install path: `C:\Program Files (x86)\Pulse-Eight\USB-CEC Adapter\`

---

## Installation

### 1. Install Python dependencies

```
pip install -r requirements.txt
```

Or let `install.bat` do it automatically (see step 3).

### 2. Configure your HDMI input

Edit `config.json` and set `hdmi_input` to the physical address of the HDMI port your PC is connected to:

| Value | Port |
|---|---|
| `"1000"` | HDMI 1 |
| `"2000"` | HDMI 2 |
| `"3000"` | HDMI 3 |
| `"4000"` | HDMI 4 |

### 3. Register the startup task

Run `install.bat` **as Administrator**. It will:
- Auto-detect `pythonw.exe`
- Install pip dependencies
- Create a Task Scheduler task that launches CEC4HTPC silently 30 seconds after login (at highest privilege, so the volume key hook works even in elevated apps)

To remove the startup task later, run `uninstall.bat`.

### 4. Test immediately

```
pythonw cec4htpc.py
```

A green TV icon will appear in the system tray.

---

## Configuration

All settings live in `config.json` (created automatically on first run if missing):

```json
{
  "hdmi_input": "2000",
  "startup_delay_seconds": 5,
  "startup_retry_count": 5,
  "startup_retry_interval_seconds": 8,
  "power_on_at_startup": true,
  "standby_on_shutdown": true,
  "standby_on_sleep": true,
  "wake_on_resume": true,
  "lock_volume": true,
  "allow_tv_to_wake_pc": false
}
```

| Key | Default | Description |
|---|---|---|
| `hdmi_input` | `"2000"` | Physical address of your PC's HDMI port |
| `startup_delay_seconds` | `5` | Seconds to wait after login before acting (lets desktop settle) |
| `startup_retry_count` | `5` | How many times to re-assert the HDMI input on startup |
| `startup_retry_interval_seconds` | `8` | Seconds between each retry |
| `power_on_at_startup` | `true` | Toggle startup TV-on behaviour |
| `standby_on_shutdown` | `true` | Toggle shutdown standby |
| `standby_on_sleep` | `true` | Toggle sleep standby |
| `wake_on_resume` | `true` | Toggle resume wake |
| `lock_volume` | `true` | Toggle volume key interception and 100% lock |
| `allow_tv_to_wake_pc` | `false` | When `false` (default), CEC is disconnected before sleep so the adapter cannot fire a USB remote-wakeup when the TV is turned on by hand. Set `true` if you want the TV power button to wake the PC. |

Changes to `config.json` take effect the next time CEC4HTPC starts. HDMI input can also be switched live from the tray menu.

---

## Beating Aggressive Devices (Apple TV)

When your PC wakes up, an Apple TV (or similar device) on the same CEC bus may also wake and broadcast `ActiveSource`, stealing the TV's active input. CEC4HTPC counters this by repeatedly re-asserting its input after startup:

```
power on → wait 2.5 s → [switch input → wait N s] × retries
```

With the defaults (5 retries, 8 s apart) this asserts your input for ~40 seconds — long enough to outlast Apple TV's startup sequence. If Apple TV is still winning, increase `startup_retry_count` or decrease `startup_retry_interval_seconds`.

---

## File Overview

```
CEC4HTPC (windows)/
├── cec4htpc.py          # Main app — tray icon and orchestration
├── cec_controller.py    # Thread-safe CEC wrapper (persistent cec-client.exe subprocess)
├── power_monitor.py     # Win32 hidden window for sleep/resume/shutdown events
├── volume_hook.py       # Volume key hook (WH_KEYBOARD_LL) + Windows volume lock (pycaw)
├── virtual_remote.py    # Bundled CEC Virtual Remote (Tkinter GUI); shares the tray app's connection
├── config.json          # User settings
├── requirements.txt     # pip dependencies
├── install.bat          # Register Task Scheduler startup task
└── uninstall.bat        # Remove startup task
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `pywin32` | Win32 API — hidden window, power events |
| `pystray` | System tray icon and menu |
| `Pillow` | Tray icon image generation |
| `keyboard` | Global `WH_KEYBOARD_LL` hook for volume key suppression |
| `pycaw` | Windows Core Audio API — master volume control |
| `comtypes` | COM interop (dependency of pycaw) |

---

## Related

- `virtual_remote.py` — Bundled CEC Virtual Remote; launched from the tray menu and shares the existing CEC connection. Can also be run standalone (`python virtual_remote.py`).
- [libcec / Pulse-Eight](https://github.com/Pulse-Eight/libcec) — underlying CEC library and `cec-client.exe`.
