#!/usr/bin/env python3
"""Exclusive gamepad routing, stable multiplayer slots and gated Linux rumble."""
import array
import ctypes
import errno
import fcntl
import glob
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import struct
import time

EVENT = struct.Struct("llHHi")
BUTTONS = {304: 0, 305: 1, 308: 2, 307: 3, 314: 4, 316: 5,
           315: 6, 317: 7, 318: 8, 310: 9, 311: 10,
           544: 11, 545: 12, 546: 13, 547: 14}
AXES = {0: 0, 1: 1, 3: 2, 4: 3, 2: 4, 5: 5}
VIRTUAL_NAME = "PC1 application controller"
MAX_PLAYERS = 4
EV_FF, EV_UINPUT, FF_RUMBLE = 21, 0x101, 0x50


def ioctl_number(direction, group, number, size):
    return direction << 30 | size << 16 | ord(group) << 8 | number


class FFEnvelope(ctypes.Structure):
    _fields_ = [(field, ctypes.c_uint16) for field in
                ("attack_length", "attack_level", "fade_length", "fade_level")]


class FFPeriodic(ctypes.Structure):
    _fields_ = [("waveform", ctypes.c_uint16), ("period", ctypes.c_uint16),
                ("magnitude", ctypes.c_int16), ("offset", ctypes.c_int16),
                ("phase", ctypes.c_uint16), ("envelope", FFEnvelope),
                ("custom_len", ctypes.c_uint32), ("custom_data", ctypes.c_void_p)]


class FFRumble(ctypes.Structure):
    _fields_ = [("strong_magnitude", ctypes.c_uint16), ("weak_magnitude", ctypes.c_uint16)]


class FFData(ctypes.Union):
    # Periodic is the largest Linux ff_effect union member; its pointer gives
    # the native ABI alignment. Only pointer-free FF_RUMBLE is forwarded.
    _fields_ = [("periodic", FFPeriodic), ("rumble", FFRumble)]


class FFEffect(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint16), ("id", ctypes.c_int16),
                ("direction", ctypes.c_uint16), ("trigger_button", ctypes.c_uint16),
                ("trigger_interval", ctypes.c_uint16), ("replay_length", ctypes.c_uint16),
                ("replay_delay", ctypes.c_uint16), ("u", FFData)]


class FFUpload(ctypes.Structure):
    _fields_ = [("request_id", ctypes.c_uint32), ("retval", ctypes.c_int32),
                ("effect", FFEffect), ("old", FFEffect)]


class FFErase(ctypes.Structure):
    _fields_ = [("request_id", ctypes.c_uint32), ("retval", ctypes.c_int32),
                ("effect_id", ctypes.c_uint32)]


EVIOCSFF = ioctl_number(1, "E", 0x80, ctypes.sizeof(FFEffect))
EVIOCRMFF = ioctl_number(1, "E", 0x81, 4)
UI_BEGIN_FF_UPLOAD = ioctl_number(3, "U", 200, ctypes.sizeof(FFUpload))
UI_END_FF_UPLOAD = ioctl_number(1, "U", 201, ctypes.sizeof(FFUpload))
UI_BEGIN_FF_ERASE = ioctl_number(3, "U", 202, ctypes.sizeof(FFErase))
UI_END_FF_ERASE = ioctl_number(1, "U", 203, ctypes.sizeof(FFErase))


def struct_ioctl(fd, request, value):
    buffer = bytearray(bytes(value))
    fcntl.ioctl(fd, request, buffer)
    ctypes.memmove(ctypes.addressof(value), bytes(buffer), len(buffer))


class Gate:
    """Held menu controls must be released before becoming game input."""
    def __init__(self):
        self.active = False
        self.blocked_buttons = set()
        self.blocked_axes = set()

    def set_active(self, active, buttons, axes):
        if active != self.active:
            self.blocked_buttons = {i for i, value in enumerate(buttons) if value}
            self.blocked_axes = {i for i, value in enumerate(axes) if abs(value) > .2}
        self.active = active

    def output(self, buttons, axes):
        self.blocked_buttons.intersection_update(i for i, value in enumerate(buttons) if value)
        self.blocked_axes.intersection_update(i for i, value in enumerate(axes) if abs(value) > .2)
        return ([int(self.active and bool(v) and i not in self.blocked_buttons and i not in (4, 5))
                 for i, v in enumerate(buttons)],
                [v if self.active and i not in self.blocked_axes else 0.0 for i, v in enumerate(axes)])


class Rumble:
    """Map application effect IDs to this slot's exclusively owned physical fd."""
    def __init__(self):
        self.device = None
        self.active = False
        self.effects = {}
        self.ids = {}

    def bind(self, device):
        self.stop()
        self.ids.clear()
        self.device = device
        self.active = False

    def set_active(self, active):
        if self.active and not active:
            self.stop()
        self.active = active

    def stop(self):
        if self.device:
            for effect_id in self.ids.values():
                try:
                    os.write(self.device["fd"], EVENT.pack(0, 0, EV_FF, effect_id, 0))
                except OSError:
                    pass  # Removed hardware cannot keep vibrating through this fd.

    def upload(self, effect):
        if not self.device:
            raise OSError(errno.ENODEV, "Controller disconnected")
        if effect.type != FF_RUMBLE or not self.device.get("rumble", False):
            raise OSError(errno.EOPNOTSUPP, "Controller has no supported rumble")
        native = FFEffect.from_buffer_copy(bytes(effect))
        native.id = self.ids.get(effect.id, -1)
        struct_ioctl(self.device["fd"], EVIOCSFF, native)
        self.ids[effect.id] = native.id
        self.effects[effect.id] = bytes(effect)

    def erase(self, effect_id):
        native = self.ids.pop(effect_id, None)
        self.effects.pop(effect_id, None)
        if native is not None and self.device:
            try:
                fcntl.ioctl(self.device["fd"], EVIOCRMFF, native)
            except OSError as error:
                if error.errno not in (errno.ENODEV, errno.EINVAL):
                    raise

    def play(self, effect_id, count):
        if not self.device or (count > 0 and not self.active):
            return
        if effect_id not in self.ids and effect_id in self.effects and count > 0:
            # Reattach uploaded effects to this same slot after reconnect. Never
            # restart playback automatically, and never send it to another slot.
            self.upload(FFEffect.from_buffer_copy(self.effects[effect_id]))
        if effect_id in self.ids:
            os.write(self.device["fd"], EVENT.pack(0, 0, EV_FF, self.ids[effect_id], max(0, count)))

    def handle(self, pad_fd, kind, code, value):
        if kind == EV_UINPUT and code in (1, 2):
            operation = FFUpload(value, 0) if code == 1 else FFErase(value, 0, 0)
            begin = UI_BEGIN_FF_UPLOAD if code == 1 else UI_BEGIN_FF_ERASE
            end = UI_END_FF_UPLOAD if code == 1 else UI_END_FF_ERASE
            struct_ioctl(pad_fd, begin, operation)
            try:
                if code == 1:
                    self.upload(operation.effect)
                else:
                    self.erase(operation.effect_id)
            except OSError as error:
                operation.retval = -(error.errno or errno.EIO)
            finally:
                # Always finish the kernel callback, including unsupported pads,
                # disconnects and upload failures; otherwise the game blocks.
                struct_ioctl(pad_fd, end, operation)
        elif kind == EV_FF:
            try:
                self.play(code, value)
            except OSError:
                self.stop()


class VirtualPad:
    def __init__(self, name=VIRTUAL_NAME, rumble=True, physical=None):
        self.fd = os.open("/dev/uinput", os.O_RDWR | os.O_NONBLOCK)
        if physical:
            fcntl.ioctl(self.fd, ioctl_number(1, "U", 108, ctypes.sizeof(ctypes.c_void_p)),
                        physical.encode() + b"\0")
        for kind in (1, 3, EV_FF) if rumble else (1, 3):
            fcntl.ioctl(self.fd, 0x40045564, kind)
        if rumble:
            fcntl.ioctl(self.fd, 0x4004556b, FF_RUMBLE)
        self.keys = {v: k for k, v in BUTTONS.items()}
        for key in self.keys.values():
            fcntl.ioctl(self.fd, 0x40045565, key)
        self.axis_codes = {v: k for k, v in AXES.items()}
        for index, code in self.axis_codes.items():
            fcntl.ioctl(self.fd, 0x40045567, code)
            minimum = 0 if index >= 4 else -32768
            fcntl.ioctl(self.fd, 0x401c5504,
                        struct.pack("H2xiiiiii", code, 0, minimum, 32767, 0, 1024, 0))
        fcntl.ioctl(self.fd, 0x405c5503,
                    struct.pack("HHHH80sI", 3, 0x1209, 0x0001, 1, name.encode(), 16 if rumble else 0))
        fcntl.ioctl(self.fd, 0x5501)
        self.buttons, self.axes = [0] * 15, [0] * 6

    def update(self, buttons, axes):
        events = []
        for i, value in enumerate(buttons):
            if value != self.buttons[i]:
                events.append(EVENT.pack(0, 0, 1, self.keys[i], value))
        integers = [round(max(-1, min(1, v)) * 32767) for v in axes]
        for i, value in enumerate(integers):
            if value != self.axes[i]:
                events.append(EVENT.pack(0, 0, 3, self.axis_codes[i], value))
        if events:
            os.write(self.fd, b"".join(events) + EVENT.pack(0, 0, 0, 0, 0))
        self.buttons, self.axes = list(buttons), integers

    def read_feedback(self, rumble):
        try:
            data = os.read(self.fd, EVENT.size * 128)
        except BlockingIOError:
            return
        for _, _, kind, code, value in EVENT.iter_unpack(data):
            rumble.handle(self.fd, kind, code, value)

    def close(self):
        self.update([0] * 15, [0.0] * 6)
        fcntl.ioctl(self.fd, 0x5502)
        os.close(self.fd)


class Slot:
    def __init__(self, index, identity=""):
        self.index, self.identity, self.device = index, identity, None
        name = VIRTUAL_NAME if index == 0 else f"{VIRTUAL_NAME} {index + 1}"
        self.pad, self.gate, self.rumble = VirtualPad(name), Gate(), Rumble()
        self.buttons, self.axes = [0] * 15, [0.] * 6

    def active(self, enabled):
        enabled = bool(enabled and self.device)
        self.gate.set_active(enabled, self.buttons, self.axes)
        self.rumble.set_active(enabled)
        self.pad.update(*self.gate.output(self.buttons, self.axes))


class Router:
    def __init__(self, directory=None):
        runtime = directory or os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        self.directory = Path(runtime) / "marwanos/controller"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        state = Path(directory) if directory else Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        self.slots_path = state / "marwanos/controller/slots.json"
        try:
            identities = json.loads(self.slots_path.read_text())
            if not isinstance(identities, list) or not all(isinstance(x, str) for x in identities):
                identities = []
        except (OSError, ValueError):
            identities = []
        self.slots = [Slot(i, identities[i] if i < len(identities) else "") for i in range(MAX_PLAYERS)]
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.setblocking(False)
        self.token = secrets.token_hex(32)
        self.devices = {}
        self.peer, self.lease, self.sequence = None, 0.0, 0
        self.app_requested, self.stopping = False, False
        self.endpoint = self.directory / "endpoint.json"
        temporary = self.endpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps({"port": self.socket.getsockname()[1], "token": self.token}))
        os.chmod(temporary, 0o600)
        temporary.replace(self.endpoint)

    @staticmethod
    def identity(fd, path, name):
        device_id = bytearray(8)
        fcntl.ioctl(fd, 0x80084502, device_id)
        for query, label in ((0x81004508, "unique"), (0x81004507, "port")):
            value = bytearray(256)
            try:
                fcntl.ioctl(fd, query, value)
            except OSError:
                continue
            value = value.split(b"\0", 1)[0].decode(errors="replace")
            if value:
                if label == "unique":
                    # DualSense reports the same MAC through USB and Bluetooth;
                    # transport bus/version must not turn it into another player.
                    _, vendor, product, _ = struct.unpack("HHHH", device_id)
                    return f"{vendor:04x}:{product:04x}:unique:{value}"
                return f"{device_id.hex()}:{label}:{value}"
        # No serial or physical location: evdev path is the only identity this
        # device supplies. Such identical pads need a stable USB port to reattach.
        return f"{device_id.hex()}:path:{path}:{name}"

    def claim(self, identity):
        for slot in self.slots:
            if slot.identity == identity and slot.device is None:
                return slot
        for slot in sorted(self.slots, key=lambda item: bool(item.identity)):
            if slot.device is None:
                if slot.identity != identity:
                    slot.rumble.effects.clear()
                slot.identity = identity
                try:
                    self.slots_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    temporary = self.slots_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps([item.identity for item in self.slots]))
                    os.chmod(temporary, 0o600)
                    temporary.replace(self.slots_path)
                except OSError as error:
                    print(f"Controller slot persistence unavailable: {error}", flush=True)
                return slot
        return None

    def scan(self):
        existing = {device["path"] for device in self.devices.values()}
        for path in sorted(glob.glob("/dev/input/event*"), key=lambda item: int(Path(item).name[5:])):
            if path in existing:
                continue
            fd = None
            try:
                writable = True
                try:
                    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                except OSError:
                    writable = False
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                name = bytearray(256)
                fcntl.ioctl(fd, 0x81004506, name)
                name = name.split(b"\0", 1)[0].decode(errors="replace")
                bits = bytearray(96)
                fcntl.ioctl(fd, 0x80604521, bits)
                if name.startswith(VIRTUAL_NAME) or not bits[304 // 8] & (1 << (304 % 8)):
                    os.close(fd)
                    continue
                ranges = {}
                for code in AXES:
                    values = array.array("i", [0] * 6)
                    try:
                        fcntl.ioctl(fd, 0x80184540 + code, values)
                        ranges[code] = (values[1], values[2])
                    except OSError:
                        pass
                if 0 not in ranges or 1 not in ranges:
                    os.close(fd)
                    continue
                identity = self.identity(fd, path, name)
                if any(slot.identity == identity and slot.device for slot in self.slots):
                    os.close(fd)
                    continue
                slot = self.claim(identity)
                if slot is None:
                    os.close(fd)
                    continue
                ff_bits = bytearray(16)
                try:
                    fcntl.ioctl(fd, ioctl_number(2, "E", 0x20 + EV_FF, 16), ff_bits)
                except OSError:
                    pass
                fcntl.ioctl(fd, 0x40044590, 1)  # EVIOCGRAB
                device = {"fd": fd, "name": name, "path": path, "ranges": ranges,
                          "sync_lost": False, "slot": slot,
                          "rumble": writable and bool(ff_bits[FF_RUMBLE // 8] & (1 << (FF_RUMBLE % 8)))}
                self.devices[fd], slot.device = device, device
                slot.rumble.bind(device)
                slot.buttons, slot.axes = [0] * 15, [0.] * 6
                self.resync(device)
                slot.active(False)
                print(f"Controller player {slot.index + 1} connected: {name}; rumble={device['rumble']}", flush=True)
            except OSError as error:
                if fd in self.devices:
                    self.disconnect(fd)
                elif fd is not None:
                    os.close(fd)
                print(f"Controller discovery failed for {path}: {error}", flush=True)

    def disconnect(self, fd):
        device = self.devices.pop(fd, None)
        if not device:
            return
        slot = device["slot"]
        slot.active(False)
        slot.rumble.bind(None)
        slot.device = None
        slot.buttons, slot.axes = [0] * 15, [0.] * 6
        slot.pad.update(slot.buttons, slot.axes)
        os.close(fd)

    def event(self, device, kind, code, value):
        slot = device["slot"]
        if device.get("sync_lost", False):
            if kind == 0 and code == 0:
                self.resync(device)
                device["sync_lost"] = False
            return
        if kind == 1 and code in BUTTONS:
            slot.buttons[BUTTONS[code]] = int(bool(value))
            if code in (314, 316) and value:
                self.set_active(False)
        elif kind == 3 and code in (16, 17):
            indices = (13, 14) if code == 16 else (11, 12)
            slot.buttons[indices[0]], slot.buttons[indices[1]] = int(value < 0), int(value > 0)
        elif kind == 3 and code in device["ranges"]:
            self.event_axis(device, code, value)
        elif kind == 0 and code == 3:
            device["sync_lost"] = True
            slot.buttons, slot.axes = [0] * 15, [0.] * 6
            slot.rumble.stop()
            slot.pad.update(slot.buttons, slot.axes)
            self.publish()

    def resync(self, device):
        fd, slot = device["fd"], device["slot"]
        keys = bytearray(96)
        fcntl.ioctl(fd, 0x80604518, keys)
        slot.buttons, slot.axes = [0] * 15, [0.] * 6
        for code, index in BUTTONS.items():
            slot.buttons[index] = int(bool(keys[code // 8] & (1 << (code % 8))))
        for code in device["ranges"]:
            values = array.array("i", [0] * 6)
            fcntl.ioctl(fd, 0x80184540 + code, values)
            self.event_axis(device, code, values[0])
        for code in (16, 17):
            values = array.array("i", [0] * 6)
            try:
                fcntl.ioctl(fd, 0x80184540 + code, values)
            except OSError:
                continue
            indices = (13, 14) if code == 16 else (11, 12)
            slot.buttons[indices[0]], slot.buttons[indices[1]] = int(values[0] < 0), int(values[0] > 0)
        active = slot.gate.active
        slot.gate.set_active(False, slot.buttons, slot.axes)
        slot.gate.set_active(active, slot.buttons, slot.axes)

    @staticmethod
    def event_axis(device, code, value):
        index = AXES[code]
        low, high = device["ranges"][code]
        value = max(0.0, min(1.0, (value - low) / max(1, high - low)))
        device["slot"].axes[index] = value if index >= 4 else value * 2 - 1

    def read_device(self, fd):
        try:
            data = os.read(fd, EVENT.size * 128)
        except BlockingIOError:
            return
        except OSError as error:
            print(f"Controller read failed: {error}", flush=True)
            self.disconnect(fd)
            return
        if not data:
            self.disconnect(fd)
            return
        device = self.devices.get(fd)
        if not device:
            return
        for _, _, kind, code, value in EVENT.iter_unpack(data):
            try:
                self.event(device, kind, code, value)
            except OSError as error:
                print(f"Controller state query failed: {error}", flush=True)
                self.disconnect(fd)
                break
            if kind == 0 and code == 0:
                slot = device["slot"]
                slot.pad.update(*slot.gate.output(slot.buttons, slot.axes))
                self.publish()

    def set_active(self, enabled):
        if any(slot.buttons[4] or slot.buttons[5] for slot in self.slots):
            enabled = False
        for slot in self.slots:
            slot.active(enabled)

    def receive(self):
        while True:
            try:
                payload, peer = self.socket.recvfrom(4096)
            except BlockingIOError:
                return
            try:
                request = json.loads(payload)
                if not isinstance(request, dict) or request.get("token") != self.token:
                    continue
                self.peer, self.lease = peer, time.monotonic() + 1.0
                self.app_requested = request.get("app") is True
                self.set_active(self.app_requested)
            except (ValueError, TypeError):
                pass

    def publish(self):
        if not self.peer or time.monotonic() > self.lease:
            return
        self.sequence += 1
        first = self.slots[0]
        buttons = list(first.buttons)
        for index in (4, 5):
            buttons[index] = int(any(slot.buttons[index] for slot in self.slots))
        # Keep the legacy first-pad fields for the shell and expose slot state
        # separately. Other players' ordinary buttons never navigate the shell.
        state = {"token": self.token, "seq": self.sequence,
                 "connected": bool(first.device), "name": first.device["name"] if first.device else "",
                 "buttons": buttons, "axes": first.axes, "home": bool(buttons[4] or buttons[5]),
                 "players": [{"slot": slot.index + 1, "connected": bool(slot.device),
                              "name": slot.device["name"] if slot.device else "",
                              "rumble": bool(slot.device and slot.device["rumble"])} for slot in self.slots]}
        try:
            self.socket.sendto(json.dumps(state).encode(), self.peer)
        except OSError:
            self.peer = None

    def run(self):
        next_scan = 0.0
        try:
            while not self.stopping:
                now = time.monotonic()
                if now >= next_scan:
                    self.scan()
                    next_scan = now + 1.0
                pads = {slot.pad.fd: slot for slot in self.slots}
                ready, _, _ = select.select([self.socket, *self.devices, *pads], [], [], 1 / 120)
                if self.socket in ready:
                    self.receive()
                # Gate before processing application FF playback. Home from any
                # pad must stop every motor before a game can enqueue more rumble.
                for fd in list(self.devices):
                    if fd in ready:
                        self.read_device(fd)
                if time.monotonic() > self.lease or any(slot.buttons[4] or slot.buttons[5] for slot in self.slots):
                    self.set_active(False)
                for fd, slot in pads.items():
                    if fd in ready:
                        slot.pad.read_feedback(slot.rumble)
                for slot in self.slots:
                    slot.pad.update(*slot.gate.output(slot.buttons, slot.axes))
                self.publish()
        finally:
            for fd in list(self.devices):
                self.disconnect(fd)
            for slot in self.slots:
                slot.pad.close()
            self.endpoint.unlink(missing_ok=True)
            self.socket.close()


if __name__ == "__main__":
    router = Router()
    def stop(_signum, _frame):
        router.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    router.run()
