# Controller routing

PC1 reserves Guide (PS/Xbox) for its own home menu. Share/Create/View is passed
to games as an ordinary controller button. D-pad and left stick
navigate; the right stick moves the compact keyboard while it is open. Keyboard
position is saved. Files, browser tabs, downloads, file uploads, installation,
minimize/resume, Close and application removal have controller-accessible controls.

The session starts `controller/router.py` as the player before starting the shell.
It exclusively grabs up to four standard Linux evdev gamepads and remembers four
player slots. Each connected physical controller gets a corresponding uinput
device bearing its physical model name, vendor ID, product ID and version.
The virtual device uses Linux `BUS_VIRTUAL` rather than claiming a physical
USB/Bluetooth transport; SDL still identifies the hardware family from its
vendor/product IDs. Empty slots expose no phantom connected devices. Unplugging a
physical pad neutralizes and destroys only its virtual device, so the game sees
a real controller disconnect and can run its own disconnect/pause behavior.
Replugging recreates that slot's device with the attached model's identity and
stable `pc1/application/slotN` physical label. A replacement model changes its
identity rather than inheriting the previous pad's type. Other connected virtual
devices are left intact. Each physical pad sends buttons, sticks,
triggers and rumble to its own application slot. The first slot drives ordinary
PC1 navigation; Guide from any slot opens the appliance menu.

Slot identities are saved atomically in
`$XDG_STATE_HOME/marwanos/controller/slots.json` (normally
`~/.local/state/marwanos/controller/slots.json`). A device serial or unique ID is
preferred, then its physical USB location. DualSense's unique MAC is independent
of USB/Bluetooth bus and firmware version. Pads without a unique ID need the same
USB port to preserve their identity; a device exposing neither identifier nor
physical location falls back to its evdev path. Unused slots are filled before
remembered disconnected slots are replaced. At most four pads are routed at
once. A disconnected first slot remains first; another player's ordinary controls
do not silently take over shell focus. Home is still available from another pad.

Rumble is forwarded through the Linux force-feedback ABI. The broker completes
uinput upload and erase callbacks, translates application effect IDs to the
correct physical file descriptor, and forwards `FF_RUMBLE` strong/weak magnitude,
duration, delay and playback count. Updating an effect reuses its physical ID.
Unsupported physical pads return `EOPNOTSUPP`; removed device handles return `ENODEV`;
driver upload errors are returned to the game without blocking a callback forever.
Other force effects and DualSense adaptive triggers are not exposed.

Opening any appliance menu, switching to pointer mode, lease expiry or shutdown
stops every active motor and neutralizes every application slot. New rumble
playback is ignored until application input is enabled. Disconnect clears both
physical and application effect IDs: they belong to the removed device. A
reconnected controller never restarts playback automatically; games upload fresh
effects to the new device. Pending uinput force-feedback requests complete with
`ENODEV` during destruction rather than delaying hotplug until their timeout.

An evdev `SYN_DROPPED` frame is recovered without disconnecting or releasing
the physical device's exclusive grab. The broker discards events through the
next `SYN_REPORT`, then queries current buttons and axes. Recovered held controls
are suppressed for applications until released. A nonblocking read with no data
also preserves the connection; actual device read failures still disconnect.

The shell receives authenticated snapshots on loopback UDP. The endpoint and
random session token live in the player's private runtime directory. Snapshots
include a sequence number; stale packets cannot restore an old button state.
`ControllerRouter` injects events into Godot and `PlayerOne` exposes the routed
axis/button state to polling controls.

`Launcher` enables application input only while a game is in the foreground.
Opening the overlay or keyboard, minimizing, closing, or losing the shell lease
neutralizes the virtual pad. Resuming suppresses controls still held from the
menu until they are released. Windows pointer mode receives shell-generated
mouse/keyboard events with the virtual gamepad held neutral. The session disables
SDL HIDAPI and filters SDL's gamepad enumeration to the virtual device so games
do not select a grabbed physical device. Applications bypassing SDL through raw
HID APIs require separate compatibility validation. Udev classifies physical
joysticks as broker-owned and exposes only `pc1/application/slotN` devices as
application joysticks. The legacy `ID_CLASS` marker also prevents SDL2 from
guessing that a hidden physical device is an application joystick. The old
fixed `0x1209/0x0001` SDL allowlist and forced generic mapping are removed.
SDL generates mappings from the virtual device's actual capabilities; Sony
`BTN_WEST/NORTH` and Xbox-style `BTN_X/Y` face-button conventions are preserved.

PC1 does not cover the game's native disconnect screen with a separate recovery
overlay. The game decides whether a real device removal pauses its current mode;
physical Tekken pause/reconnect behavior still requires in-game acceptance.

## Verification

`tests/test_controller_router.py` tests routing policy, input queue overflow
recovery and nonblocking reads. Run
`scripts/check-controller-shell.sh` with the pinned Godot editor to verify routed
actions, axes, player ownership and disconnect recovery in the actual shell.

`sudo python3 tests/controller_kernel.py` creates three temporary synthetic Linux
gamepads: two rumble-capable devices and one unsupported device. It verifies real
exclusive grabs, shell delivery, independent application events, held-button
suppression, all-pad overlay/Home/lease neutralization, effect upload/update,
correct motor destination, cancellation, erase, driver errors and stable reconnect.
The October 7 hotplug fixture also verifies removed application handles return
`ENODEV`, an unused fourth slot has no virtual device, replug restores fresh
input/rumble without held-button leakage, and player two keeps the same device
and working input throughout player one's removal. A queued FF upload is failed
promptly by real device destruction. The fixture also verifies hardware model
names and IDs reach the virtual devices. All 35 policy tests and that kernel fixture
pass; this is not a physical Tekken pause result.
`sudo python3 tests/controller_sdl.py` additionally exercises real uinput devices
through SDL2: DualSense reports PS5, Xbox 360 reports Xbox 360, and unknown
hardware retains its own IDs. All routed face buttons, D-pad, sticks and triggers
are checked without a forced mapping. The live PC1 player SDL probe enumerates
exactly one routed DualSense (`054c:0ce6`, PS5 type), excluding its physical node.
It runs in the Linux build VM and does not require a physical controller. The
23 policy/recovery tests and this kernel fixture passed in Fedora WSL on 2026-10-06.
Physical DualSense rumble, a second physical controller, and Bluetooth reattachment
still require hardware acceptance; synthetic FF callbacks cannot prove vibration
or a game's local multiplayer behavior. Tekken's single physical DualSense input
was independently confirmed by the user before these changes.

The force-feedback implementation follows the
[Linux input force-feedback documentation](https://docs.kernel.org/input/ff.html)
and the kernel's
[uinput callback ABI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/uinput.h).

Boot success now requires a recent `shell.ready` heartbeat written only after the
home screen is assembled. A live greetd process around a failed shell no longer
marks the boot successful.
