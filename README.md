# pc1-controllers

PC1 exclusive Linux input broker and virtual application controller routing.

Part of the [PC1 project](https://github.com/users/MarwanSummakieh/projects/3).
[MarwanOS](https://github.com/MarwanSummakieh/MarwanOS) builds the complete OS and
integrates an exact pinned export of this component. This repository owns its
implementation; the Godot UI for backend components lives in pc1-shell.

Source organization date: 2026-10-06. The exported implementation includes the
2026-10-05 development work from MarwanOS commit a634e6ce3001f197524daa8cf357b160383890db.
Existing relative paths are retained so component Python tests can run directly.

See the documentation in docs/ for behavior, evidence and current limitations.
For cross-component tests and complete image builds, use the PC1 OS workspace.
The OS repository's pc1-components.json records the integrated commit and file
ownership; scripts/components.py checks or restores the pinned integration copies.
## Checks

```bash
python3 -m unittest discover -s tests -v
```

Run these tests on Linux. Installer icon tests require pefile/Pillow and the
installer fixtures require Xvfb. Audio/notification integration requires the
PipeWire/PulseAudio and D-Bus dependencies documented in the component reports.
Controller kernel acceptance additionally needs evdev/uinput device permissions.
