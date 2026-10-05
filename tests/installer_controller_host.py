#!/usr/bin/env python3
"""Host gamepad fixture for forwarding into a disposable installer VM.

Run serve as root on the QEMU host, then forward ready.device with:
  -device virtio-input-host-pci,evdev=/dev/input/eventN

Commands emit real Linux evdev input; they never call QMP input, GTK, or the
installer's storage machinery. See docs/installer-controller-acceptance.md.
"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import struct
import tempfile
import time

EVENT = struct.Struct("llHHi")
NAME = "PC1 installer acceptance gamepad"
SOCKET = "/run/pc1-installer-controller-host.sock"
BUTTONS = {"cross": 304, "a": 304, "circle": 305, "b": 305,
           "square": 308, "x": 308, "triangle": 307, "y": 307,
           "l1": 310, "r1": 311, "options": 315}
DIRECTIONS = {"left": (16, -1), "right": (16, 1),
              "up": (17, -1), "down": (17, 1)}
AXES = {"left_x": 0, "left_y": 1}


class Gamepad:
    def __init__(self):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        for kind in (1, 3):
            fcntl.ioctl(self.fd, 0x40045564, kind)
        for key in set(BUTTONS.values()):
            fcntl.ioctl(self.fd, 0x40045565, key)
        for axis in (0, 1, 16, 17):
            fcntl.ioctl(self.fd, 0x40045567, axis)
            low, high = (-32768, 32767) if axis < 2 else (-1, 1)
            fcntl.ioctl(self.fd, 0x401c5504,
                        struct.pack("H2xiiiiii", axis, 0, low, high, 0, 0, 0))
        fcntl.ioctl(self.fd, 0x405c5503,
                    struct.pack("HHHH80sI", 3, 0x1209, 0x0002, 1, NAME.encode(), 0))
        fcntl.ioctl(self.fd, 0x5501)
        self.keys, self.axes, self.directions = set(), {}, set()

    def emit(self, kind, code, value):
        os.write(self.fd, EVENT.pack(0, 0, kind, code, value) + EVENT.pack(0, 0, 0, 0, 0))

    def button(self, name, pressed):
        if name in DIRECTIONS:
            if pressed:
                self.directions.add(name)
            else:
                self.directions.discard(name)
            axis, _ = DIRECTIONS[name]
            value = sum(direction for item, (code, direction) in DIRECTIONS.items()
                        if code == axis and item in self.directions)
            self.axis(axis, value)
        else:
            code = BUTTONS[name]
            self.emit(1, code, int(pressed))
            if pressed:
                self.keys.add(code)
            else:
                self.keys.discard(code)

    def axis(self, code, value):
        self.axes[code] = value
        self.emit(3, code, value)

    def neutral(self):
        for key in list(self.keys):
            self.emit(1, key, 0)
        for axis in (0, 1, 16, 17):
            self.axis(axis, 0)
        self.keys.clear()
        self.directions.clear()

    def close(self):
        self.neutral()
        fcntl.ioctl(self.fd, 0x5502)
        os.close(self.fd)


def device_path():
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for entry in Path("/sys/class/input").glob("event*"):
            try:
                if (entry / "device/name").read_text().strip() == NAME:
                    return "/dev/input/" + entry.name
            except OSError:
                pass
        time.sleep(.02)
    raise RuntimeError("host fixture evdev device did not appear")


def serve(args):
    if Path(args.socket).exists():
        raise RuntimeError("fixture socket exists; stop its owner first")
    pad = Gamepad()
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        path = device_path()
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection, \
                open(args.evidence, "a", encoding="utf-8", buffering=1) as evidence:
            connection.bind(args.socket)
            os.chmod(args.socket, 0o600)
            ready = {"device": path, "pid": os.getpid(), "name": NAME}
            if args.device_file:
                Path(args.device_file).write_text(path + "\n")
            evidence.write(json.dumps({"ready": ready, "timestamp": time.time()}) + "\n")
            print(json.dumps({"ready": ready}), flush=True)
            deadline = time.monotonic() + args.lifetime
            while not stopping and time.monotonic() < deadline:
                if not select.select([connection], [], [], .25)[0]:
                    continue
                payload, peer = connection.recvfrom(4096)
                try:
                    command = json.loads(payload)
                    if not isinstance(command, dict):
                        raise ValueError("command must be an object")
                    op = command.get("op")
                    duration = float(command.get("duration", .12))
                    if not math.isfinite(duration) or not 0 <= duration <= 5:
                        raise ValueError("duration must be between 0 and 5 seconds")
                    if op in ("tap", "press", "release"):
                        name = command.get("name")
                        if name not in BUTTONS and name not in DIRECTIONS:
                            raise ValueError("unknown button")
                        pad.button(name, op != "release")
                        if op == "tap":
                            time.sleep(duration)
                            pad.button(name, False)
                    elif op == "axis":
                        name, value = command.get("name"), float(command.get("value", 0))
                        if name not in AXES or not math.isfinite(value) or not -1 <= value <= 1:
                            raise ValueError("axis requires left_x/left_y and value between -1 and 1")
                        pad.axis(AXES[name], round(value * 32767))
                        time.sleep(duration)
                        pad.axis(AXES[name], 0)
                    elif op in ("neutral", "stop"):
                        pad.neutral()
                        stopping = op == "stop"
                    elif op != "status":
                        raise ValueError("unknown operation")
                    response = {"ok": True, "command": command, "device": path,
                                "held_keys": sorted(pad.keys), "axes": pad.axes}
                except (ValueError, TypeError, OSError) as error:
                    response = {"ok": False, "error": str(error)}
                response["timestamp"] = time.time()
                evidence.write(json.dumps(response, sort_keys=True) + "\n")
                try:
                    connection.sendto(json.dumps(response).encode(), peer)
                except OSError:
                    pass
    finally:
        pad.close()
        Path(args.socket).unlink(missing_ok=True)


def client(args):
    command = {"op": args.op, "name": args.name, "value": args.value,
               "duration": args.duration}
    with tempfile.TemporaryDirectory(prefix="pc1-installer-pad-client-") as temporary, \
            socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
        connection.bind(str(Path(temporary) / "reply.sock"))
        connection.settimeout(7)
        connection.sendto(json.dumps(command).encode(), args.socket)
        response = json.loads(connection.recv(8192))
    print(json.dumps(response, sort_keys=True))
    return 0 if response.get("ok") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("op", choices=("serve", "status", "tap", "press", "release", "axis", "neutral", "stop"))
    parser.add_argument("name", nargs="?")
    parser.add_argument("value", nargs="?", type=float)
    parser.add_argument("--socket", default=SOCKET)
    parser.add_argument("--duration", type=float, default=.12)
    parser.add_argument("--evidence", default="/var/tmp/pc1-installer-controller-host.jsonl")
    parser.add_argument("--device-file")
    parser.add_argument("--lifetime", type=float, default=1800)
    args = parser.parse_args()
    if args.op == "serve":
        if not math.isfinite(args.lifetime) or not 1 <= args.lifetime <= 7200:
            parser.error("lifetime must be between 1 and 7200 seconds")
        serve(args)
        return 0
    return client(args)


if __name__ == "__main__":
    raise SystemExit(main())
