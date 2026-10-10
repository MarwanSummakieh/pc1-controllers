#!/usr/bin/env python3
"""Drive a booted PC1 VM through real Linux uinput and its running broker.

Guest (root):
  python3 /tmp/vm_controller.py serve --socket /run/pc1-vm-controller.sock \
      --evidence /tmp/pc1-vm-controller.jsonl
  python3 /tmp/vm_controller.py status
  python3 /tmp/vm_controller.py tap cross
  python3 /tmp/vm_controller.py tap right
  python3 /tmp/vm_controller.py axis right_x -0.8 --duration 0.5
  python3 /tmp/vm_controller.py stop

This helper never sends UDP broker commands or Godot events. Its fixture is an
actual kernel evdev device with a distinct name. The existing session broker
must discover and grab it. Evidence includes an independent physical reader and
an application-pad reader, making unintended input leakage observable.
"""

import argparse
import errno
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import socket
import tempfile
import time

SOCKET = "/run/pc1-vm-controller.sock"
NAME = "PC1 VM acceptance controller"
BUTTONS = {
    "cross": 0, "a": 0, "circle": 1, "b": 1,
    "square": 2, "x": 2, "triangle": 3, "y": 3,
    "share": 4, "guide": 5, "home": 5, "options": 6,
    "l3": 7, "r3": 8, "l1": 9, "r1": 10,
    "up": 11, "down": 12, "left": 13, "right": 14,
}
AXES = {"left_x": 0, "left_y": 1, "right_x": 2, "right_y": 3, "l2": 4, "r2": 5}
# Independent Linux/DualSense semantics: do not derive the physical fixture's
# Square/Triangle codes from the very broker mapping this acceptance tests.
LINUX_BUTTON_CODES = {0: 304, 1: 305, 2: 308, 3: 307, 4: 314, 5: 316,
                      6: 315, 7: 317, 8: 318, 9: 310, 10: 311,
                      11: 544, 12: 545, 13: 546, 14: 547}


def load_router(path):
    spec = importlib.util.spec_from_file_location("pc1_vm_router", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def device_path(name, timeout=5, physical=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for path in Path("/sys/class/input").glob("event*"):
            try:
                if ((path / "device/name").read_text().strip() == name
                        and (physical is None or (path / 'device/phys').read_text().strip() == physical)):
                    return "/dev/input/" + path.name
            except OSError:
                continue
        time.sleep(.02)
    raise RuntimeError("uinput device did not appear: " + name)


def read_events(fd, event_struct):
    result = []
    if fd is None:
        return result
    while select.select([fd], [], [], 0)[0]:
        try:
            data = os.read(fd, event_struct.size * 128)
        except BlockingIOError:
            break
        if not data:
            break
        result.extend([kind, code, value] for _, _, kind, code, value in event_struct.iter_unpack(data))
    return result


def exclusive_grabbed(fd):
    """A separate reader's EVIOCGRAB must fail while the real broker owns it."""
    try:
        fcntl.ioctl(fd, 0x40044590, 1)
    except OSError as error:
        if error.errno == errno.EBUSY:
            return True
        raise
    fcntl.ioctl(fd, 0x40044590, 0)
    return False


class Fixture:
    def __init__(self, args):
        self.router = load_router(args.router)
        self.pad = self.router.VirtualPad(args.name, input_id=(3, 0x054c, 0x0ce6, 0x8111))
        self.pad.keys = dict(LINUX_BUTTON_CODES)
        self.physical_path = device_path(args.name)
        self.physical = os.open(self.physical_path, os.O_RDONLY | os.O_NONBLOCK)
        self.application_path = device_path(args.name, physical='pc1/application/slot1')
        self.application = os.open(self.application_path, os.O_RDONLY | os.O_NONBLOCK)
        self.buttons = [0] * 15
        self.axes = [0.] * 6
        self.stopping = False
        self.socket_path = Path(args.socket)
        if self.socket_path.exists():
            raise RuntimeError("fixture socket already exists; stop its existing owner first")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.evidence = open(args.evidence, "a", encoding="utf-8", buffering=1)
        self.name = args.name
        self.deadline = time.monotonic() + args.lifetime

    def write_evidence(self, data):
        data["timestamp"] = time.time()
        self.evidence.write(json.dumps(data, sort_keys=True) + "\n")

    def status(self):
        return {
            "name": self.name, "pid": os.getpid(),
            "physical_path": self.physical_path,
            "application_path": self.application_path,
            "broker_exclusive_grab": exclusive_grabbed(self.physical),
            "buttons": self.buttons, "axes": self.axes,
            "linux_button_codes": LINUX_BUTTON_CODES,
        }

    def publish(self):
        self.pad.update(self.buttons, self.axes)

    def neutral(self):
        self.buttons = [0] * 15
        self.axes = [0.] * 6
        self.publish()

    def command(self, command):
        if not isinstance(command, dict):
            raise ValueError("command must be a JSON object")
        op = command.get("op")
        duration = float(command.get("duration", .12 if op == "tap" else 0))
        if not 0 <= duration <= 2:
            raise ValueError("duration must be between 0 and 2 seconds")
        before_physical = read_events(self.physical, self.router.EVENT)
        before_application = read_events(self.application, self.router.EVENT)
        if op in ("tap", "press", "release"):
            name = command.get("button")
            if name not in BUTTONS:
                raise ValueError("unknown button")
            index = BUTTONS[name]
            self.buttons[index] = int(op != "release")
            self.publish()
            if op == "tap":
                time.sleep(duration)
                self.buttons[index] = 0
                self.publish()
        elif op == "axis":
            name = command.get("axis")
            if name not in AXES:
                raise ValueError("unknown axis")
            index = AXES[name]
            value = float(command.get("value", 0))
            low = 0 if index >= 4 else -1
            if not low <= value <= 1:
                raise ValueError("axis value is outside its physical range")
            self.axes[index] = value
            self.publish()
            if duration:
                time.sleep(duration)
                self.axes[index] = 0
                self.publish()
        elif op in ("neutral", "stop"):
            self.neutral()
            self.stopping = op == "stop"
        elif op != "status":
            raise ValueError("unknown operation")
        time.sleep(.08)
        data = {
            "ok": True, "command": command, "status": self.status(),
            "physical_events": read_events(self.physical, self.router.EVENT),
            "application_events": read_events(self.application, self.router.EVENT),
            "prior_physical_events": before_physical,
            "prior_application_events": before_application,
        }
        self.write_evidence(data)
        return data

    def run(self):
        # Allow the production broker's one-second discovery loop to claim it.
        time.sleep(2)
        ready = self.status()
        self.write_evidence({"ready": ready})
        print(json.dumps({"ready": ready}), flush=True)
        while not self.stopping and time.monotonic() < self.deadline:
            if not select.select([self.socket], [], [], .25)[0]:
                continue
            payload, peer = self.socket.recvfrom(8192)
            try:
                response = self.command(json.loads(payload))
            except (ValueError, TypeError, KeyError, OSError) as error:
                response = {"ok": False, "error": str(error)}
                self.write_evidence(response)
            try:
                self.socket.sendto(json.dumps(response).encode(), peer)
            except OSError as error:
                self.write_evidence({"client_disconnected": str(error)})

    def close(self):
        try:
            self.neutral()
            time.sleep(.05)
            self.pad.close()
        finally:
            for fd in (self.physical, self.application):
                os.close(fd)
            self.socket.close()
            self.socket_path.unlink(missing_ok=True)
            self.evidence.close()


def client(args):
    command = {"op": args.op}
    if args.op in ("tap", "press", "release"):
        command["button"] = args.name
    elif args.op == "axis":
        command.update(axis=args.name, value=args.value)
    if args.duration is not None:
        command["duration"] = args.duration
    with tempfile.TemporaryDirectory(prefix="pc1-vm-controller-client-") as temporary:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
            connection.bind(str(Path(temporary) / "reply.sock"))
            connection.settimeout(5)
            connection.sendto(json.dumps(command).encode(), args.socket)
            response = json.loads(connection.recv(65536))
    print(json.dumps(response, sort_keys=True))
    return 0 if response.get("ok") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("op", choices=["serve", "status", "tap", "press", "release", "axis", "neutral", "stop"])
    parser.add_argument("name", nargs="?")
    parser.add_argument("value", nargs="?", type=float)
    parser.add_argument("--socket", default=SOCKET)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--router", default="/usr/lib/marwanos/controller/router.py")
    parser.add_argument("--fixture-name", default=NAME)
    parser.add_argument("--evidence", default="/tmp/pc1-vm-controller.jsonl")
    parser.add_argument("--lifetime", type=float, default=1800)
    args = parser.parse_args()
    if args.op in ("tap", "press", "release") and args.name not in BUTTONS:
        parser.error("choose a named controller button")
    if args.op == "axis" and (args.name not in AXES or args.value is None):
        parser.error("axis requires a named stick/trigger and a numeric value")
    if args.op != "serve":
        return client(args)
    args.name = args.fixture_name
    fixture = Fixture(args)
    signal.signal(signal.SIGTERM, lambda *_args: setattr(fixture, "stopping", True))
    signal.signal(signal.SIGINT, lambda *_args: setattr(fixture, "stopping", True))
    try:
        fixture.run()
    finally:
        fixture.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
