# Controller routing

PC1 reserves Guide and Share/View for its own home menu. D-pad and left stick
navigate; the right stick moves the compact keyboard while it is open. Keyboard
position is saved. Files, browser tabs, downloads, file uploads, installation,
minimize/resume, Close and application removal have controller-accessible controls.

The session starts `controller/router.py` as the player before starting the shell.
It exclusively grabs standard Linux evdev gamepads and creates a virtual
`PC1 application controller` through uinput. The first connected controller drives
PC1; additional physical pads are held inactive. Disconnect releases every control
and another connected pad can take over. Rumble and multiplayer routing are not
implemented.

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
HID APIs require separate compatibility validation.

## Verification

`tests/test_controller_router.py` tests routing policy, input queue overflow
recovery and nonblocking reads. Run
`scripts/check-controller-shell.sh` with the pinned Godot editor to verify routed
actions, axes, player ownership and disconnect recovery in the actual shell.

`sudo python3 tests/controller_kernel.py` creates temporary synthetic Linux
gamepads and verifies the real exclusive grab, shell delivery, virtual application
events, held-button suppression, overlay release and lease expiry. It runs in the
Linux build VM and does not require a physical controller. Physical DualSense/Xbox
testing remains necessary for device-specific mappings and hardware behavior.

Boot success now requires a recent `shell.ready` heartbeat written only after the
home screen is assembled. A live greetd process around a failed shell no longer
marks the boot successful.
