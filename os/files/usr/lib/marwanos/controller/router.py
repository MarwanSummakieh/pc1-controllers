#!/usr/bin/env python3
"""Route a physical Linux gamepad to PC1 and a gated virtual application pad.

Physical evdev devices are exclusively grabbed. The shell receives full state
over authenticated loopback UDP; applications receive only the virtual pad.
A one-second shell lease fails closed and releases every virtual control.
"""
import array
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


class Gate:
    """Pure routing policy, independently tested without kernel input devices."""
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
        # Share and Guide always belong to the appliance, including in games.
        return ([int(self.active and bool(v) and i not in self.blocked_buttons and i not in (4, 5))
                 for i, v in enumerate(buttons)],
                [v if self.active and i not in self.blocked_axes else 0.0 for i, v in enumerate(axes)])


class VirtualPad:
    def __init__(self, name=VIRTUAL_NAME):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        for kind in (1, 3):
            fcntl.ioctl(self.fd, 0x40045564, kind)
        self.keys = {v: k for k, v in BUTTONS.items()}
        for key in self.keys.values():
            fcntl.ioctl(self.fd, 0x40045565, key)
        self.axis_codes = {v: k for k, v in AXES.items()}
        for index, code in self.axis_codes.items():
            fcntl.ioctl(self.fd, 0x40045567, code)
            minimum = 0 if index >= 4 else -32768
            # uinput_abs_setup: u16 code, alignment, struct input_absinfo.
            fcntl.ioctl(self.fd, 0x401c5504,
                        struct.pack("H2xiiiiii", code, 0, minimum, 32767, 0, 1024, 0))
        fcntl.ioctl(self.fd, 0x405c5503,
                    struct.pack("HHHH80sI", 3, 0x1209, 0x0001, 1, name.encode(), 0))
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

    def close(self):
        self.update([0] * 15, [0.0] * 6)
        fcntl.ioctl(self.fd, 0x5502)
        os.close(self.fd)


class Router:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "marwanos/controller"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.setblocking(False)
        self.token = secrets.token_hex(32)
        self.pad = VirtualPad()
        self.devices = {}
        self.device = None
        self.buttons, self.axes = [0] * 15, [0.0] * 6
        self.gate = Gate()
        self.peer = None
        self.lease = 0.0
        self.sequence = 0
        self.stopping = False
        self.endpoint = self.directory / "endpoint.json"
        temporary = self.endpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps({"port": self.socket.getsockname()[1], "token": self.token}))
        os.chmod(temporary, 0o600)
        temporary.replace(self.endpoint)

    def scan(self):
        existing = {device["path"] for device in self.devices.values()}
        for path in sorted(glob.glob("/dev/input/event*")):
            if path in existing:
                continue
            fd = None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                name = bytearray(256)
                fcntl.ioctl(fd, 0x81004506, name)
                name = name.split(b"\0", 1)[0].decode(errors="replace")
                bits = bytearray(96)
                fcntl.ioctl(fd, 0x80604521, bits)
                if name == VIRTUAL_NAME or not bits[304 // 8] & (1 << (304 % 8)):
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
                fcntl.ioctl(fd, 0x40044590, 1)  # EVIOCGRAB
                device = {"fd": fd, "name": name, "path": path, "ranges": ranges, "sync_lost": False}
                self.devices[fd] = device
                if self.device is None:
                    self.device = device
                    self.buttons, self.axes = [0] * 15, [0.0] * 6
                    self.gate.set_active(False, self.buttons, self.axes)
                    print(f"Controller connected: {name}", flush=True)
            except OSError:
                if fd is not None:
                    os.close(fd)

    def disconnect(self, fd=None):
        fd = self.device["fd"] if fd is None and self.device else fd
        if fd in self.devices:
            os.close(fd)
            self.devices.pop(fd)
        if self.device and self.device["fd"] != fd:
            return
        self.device = next(iter(self.devices.values()), None)
        self.buttons, self.axes = [0] * 15, [0.0] * 6
        self.gate.set_active(False, self.buttons, self.axes)
        self.pad.update(self.buttons, self.axes)

    def event(self, kind, code, value):
        if self.device.get("sync_lost", False):
            # An evdev queue overrun is not a physical disconnect. Discard the
            # incomplete frame, then query the kernel at the next SYN_REPORT.
            if kind == 0 and code == 0:
                self.resync()
                self.device["sync_lost"] = False
            return
        if kind == 1 and code in BUTTONS:
            self.buttons[BUTTONS[code]] = int(bool(value))
            if code in (314, 316) and value:
                self.gate.set_active(False, self.buttons, self.axes)
        elif kind == 3 and code in (16, 17):
            indices = (13, 14) if code == 16 else (11, 12)
            self.buttons[indices[0]], self.buttons[indices[1]] = int(value < 0), int(value > 0)
        elif kind == 3 and code in self.device["ranges"]:
            self.event_axis(code, value)
        # Keep the exclusive grab and identity while recovering a lost frame.
        elif kind == 0 and code == 3:
            self.device["sync_lost"] = True
            self.buttons, self.axes = [0] * 15, [0.0] * 6
            self.pad.update(self.buttons, self.axes)
            self.publish()

    def resync(self):
        fd = self.device["fd"]
        keys = bytearray(96)
        fcntl.ioctl(fd, 0x80604518, keys)  # EVIOCGKEY
        self.buttons = [0] * 15
        self.axes = [0.0] * 6
        for code, index in BUTTONS.items():
            self.buttons[index] = int(bool(keys[code // 8] & (1 << (code % 8))))
        for code in self.device["ranges"]:
            values = array.array("i", [0] * 6)
            fcntl.ioctl(fd, 0x80184540 + code, values)  # EVIOCGABS
            self.event_axis(code, values[0])
        for code in (16, 17):
            values = array.array("i", [0] * 6)
            try:
                fcntl.ioctl(fd, 0x80184540 + code, values)
            except OSError:
                continue  # Pads may expose the D-pad as buttons instead.
            indices = (13, 14) if code == 16 else (11, 12)
            self.buttons[indices[0]], self.buttons[indices[1]] = int(values[0] < 0), int(values[0] > 0)
        # A recovered held control must not become a new application press.
        active = self.gate.active
        self.gate.set_active(False, self.buttons, self.axes)
        self.gate.set_active(active, self.buttons, self.axes)

    def event_axis(self, code, value):
        index = AXES[code]
        low, high = self.device["ranges"][code]
        value = max(0.0, min(1.0, (value - low) / max(1, high - low)))
        self.axes[index] = value if index >= 4 else value * 2 - 1

    def read_device(self, fd):
        try:
            data = os.read(fd, EVENT.size * 128)
        except BlockingIOError:
            return  # A nonblocking read without data does not remove the pad.
        except OSError as error:
            print(f"Controller read failed: {error}", flush=True)
            self.disconnect(fd)
            return
        if not data:
            self.disconnect(fd)
        elif self.device and self.device["fd"] == fd:
            for _, _, kind, code, value in EVENT.iter_unpack(data):
                if self.device:
                    try:
                        self.event(kind, code, value)
                    except OSError as error:
                        print(f"Controller state query failed: {error}", flush=True)
                        self.disconnect(fd)
                        break
                    if kind == 0 and code == 0:
                        self.pad.update(*self.gate.output(self.buttons, self.axes))
                        self.publish()

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
                self.gate.set_active(request.get("app") is True and self.device is not None,
                                     self.buttons, self.axes)
            except (ValueError, TypeError):
                pass

    def publish(self):
        if not self.peer or time.monotonic() > self.lease:
            return
        self.sequence += 1
        state = {"token": self.token, "seq": self.sequence, "connected": bool(self.device),
                 "name": self.device["name"] if self.device else "",
                 "buttons": self.buttons, "axes": self.axes}
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
                descriptors = [self.socket, *self.devices]
                ready, _, _ = select.select(descriptors, [], [], 1 / 120)
                if self.socket in ready:
                    self.receive()
                for fd in list(self.devices):
                    if fd not in ready:
                        continue
                    self.read_device(fd)
                if now > self.lease:
                    self.gate.set_active(False, self.buttons, self.axes)
                # Home takes effect before a shell frame can open its overlay.
                if self.buttons[4] or self.buttons[5]:
                    self.gate.set_active(False, self.buttons, self.axes)
                self.pad.update(*self.gate.output(self.buttons, self.axes))
                self.publish()
        finally:
            for fd in list(self.devices):
                self.disconnect(fd)
            self.pad.close()
            self.endpoint.unlink(missing_ok=True)
            self.socket.close()


if __name__ == "__main__":
    router = Router()
    def stop(_signum, _frame):
        router.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    router.run()
