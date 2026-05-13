# Windows installer

End-users get a single `PioneerDJLink-Setup.exe`. Double-click → Next → Done.
Python is bundled — they do **not** need to install anything else.

---

## One-time setup on the build machine

1. Install **Python 3.11 or 3.12** for Windows (`https://www.python.org/downloads/windows/`)
   - During install tick *"Add python.exe to PATH"*.
2. Install **Inno Setup 6** (`https://jrsoftware.org/isdl.php`).
   - Default install location is auto-detected by the build script.
3. (Optional) Drop a `app.ico` next to this README to brand the EXE / installer.

That's it — no Visual Studio, no SDKs.

---

## Build

From a PowerShell prompt **in the repo root**:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

Useful flags:

| Flag              | Effect                                                          |
| ----------------- | --------------------------------------------------------------- |
| `-Clean`          | Wipe `build\`, `dist\`, and `Output\` before building.          |
| `-SkipInstaller`  | Stop after PyInstaller (gives you a portable `dist\` folder).   |

Outputs:

- `dist\PioneerDJLink\PioneerDJLink.exe` — portable build (zip & ship as-is).
- `packaging\windows\Output\PioneerDJLink-Setup.exe` — the installer.

---

## What the installer does for the user

- Copies the app to `%ProgramFiles%\Pioneer DJ Link\`.
- Adds Start-menu and (optional) desktop shortcuts.
- Opens **UDP 50000–50002** in the Windows firewall (required for the
  DJ Link discovery / beat / status broadcasts).
- Cleans up firewall rules on uninstall.

---

## Notes / caveats

- `requirements-windows.txt` is intentionally a subset of `requirements.txt`:
  `aubio`, `sounddevice`, and `python-rtmidi` are listed in the dev deps but
  are **not currently imported anywhere in the codebase**, and they're a pain
  to wheel-install on Windows. If you start using them, add them here and
  remove them from the `excludes` list in `app.spec`.
- Python 3.13 works in principle but PyQt6 wheels for new Python versions
  sometimes lag a release; if the build fails on `pip install PyQt6`, fall
  back to Python 3.12.
- The CDJ-3000 expects the host to be reachable on the *same* IPv4 subnet as
  the players. If the user has multiple network interfaces, instruct them
  to disable Wi-Fi while plugged into the Pioneer switch.
