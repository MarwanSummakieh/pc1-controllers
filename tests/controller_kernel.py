#!/usr/bin/env python3
"""Real evdev/uinput routing acceptance. Run in a Linux test VM as root."""
import importlib.util
import json
import os
from pathlib import Path
import select
import socket
import tempfile
import threading
import time
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("router", Path(__file__).resolve().parents[1] /
    "os/files/usr/lib/marwanos/controller/router.py")
router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router)


def device_path(name):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for path in Path("/sys/class/input").glob("event*"):
            if (path / "device/name").read_text().strip() == name:
                return "/dev/input/" + path.name
        time.sleep(.02)
    raise AssertionError("uinput device did not appear: " + name)


def events(fd, duration=.1):
    result = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], max(0, deadline - time.monotonic()))
        if readable:
            result.extend(router.EVENT.iter_unpack(os.read(fd, router.EVENT.size * 128)))
    return result


with tempfile.TemporaryDirectory(prefix="pc1-controller-kernel-") as temporary:
    physical = router.VirtualPad("PC1 router fixture")
    path = device_path("PC1 router fixture")
    observer = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    broker = router.Router(temporary)
    output = os.open(device_path(router.VIRTUAL_NAME), os.O_RDONLY | os.O_NONBLOCK)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(2)
    endpoint = json.loads(broker.endpoint.read_text())
    address = ("127.0.0.1", endpoint["port"])
    def mode(app):
        client.sendto(json.dumps({"token": endpoint["token"], "app": app}).encode(), address)
        time.sleep(.03)
    thread = threading.Thread(target=broker.run)
    try:
        with patch.object(router.glob, "glob", return_value=[path]):
            thread.start()
            mode(False)
            buttons, axes = [0] * 15, [0.] * 6
            buttons[0] = 1
            physical.update(buttons, axes)
            time.sleep(.04)
            assert not [e for e in events(observer) if e[2] == 1], "physical events escaped the exclusive grab"
            assert not [e for e in events(output) if e[2] == 1 and e[4]], "menu input reached application"
            got_press = False
            while not got_press:
                state = json.loads(client.recv(8192))
                got_press = state.get("buttons", [0])[0] == 1
            mode(True)
            assert not [e for e in events(output) if e[2] == 1 and e[4]], "held confirm leaked on resume"
            buttons[0] = 0
            physical.update(buttons, axes)
            time.sleep(.03)
            buttons[0] = 1
            physical.update(buttons, axes)
            assert any(e[2:] == (1, 304, 1) for e in events(output)), "application did not receive fresh A"
            mode(False)
            assert any(e[2:] == (1, 304, 0) for e in events(output)), "opening overlay did not release A"
            buttons[0] = 0
            physical.update(buttons, axes)
            time.sleep(.03)
            mode(True)
            buttons[0] = 1
            physical.update(buttons, axes)
            events(output)
            assert any(e[2:] == (1, 304, 0) for e in events(output, 1.2)), "expired shell lease did not release A"
            print("Controller kernel checks: exclusive grab, shell delivery, application gating, held-button suppression, overlay release and lease expiry PASS")
    finally:
        broker.stopping = True
        thread.join(3)
        physical.close()
        os.close(observer)
        os.close(output)
        client.close()
