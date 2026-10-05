#!/usr/bin/env python3
"""Run inside the installer VM: emulate a gamepad and verify bridge output."""
import fcntl
import glob
import os
from pathlib import Path
import select
import struct
import time

event = struct.Struct("llHHi")
gamepad = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
for kind in (1, 3):
    fcntl.ioctl(gamepad, 0x40045564, kind)
for key in (304, 305, 307, 308, 310, 311, 315):
    fcntl.ioctl(gamepad, 0x40045565, key)
for axis in (0, 1, 16, 17):
    fcntl.ioctl(gamepad, 0x40045567, axis)
    low, high = (-32768, 32767) if axis < 2 else (-1, 1)
    fcntl.ioctl(gamepad, 0x401c5504, struct.pack("H2xiiiiii", axis, 0, low, high, 0, 0, 0))
fcntl.ioctl(gamepad, 0x405c5503, struct.pack("HHHH80sI", 3, 0x1209, 2, 1, b"PC1 test gamepad", 0))
fcntl.ioctl(gamepad, 0x5501)
time.sleep(4)
outputs = [path for path in glob.glob("/sys/class/input/event*/device/name")
           if Path(path).read_text().strip() == "PC1 installer controller"]
assert len(outputs) == 1, outputs
device_name = Path(outputs[0]).parents[1].name
reader = os.open("/dev/input/" + device_name, os.O_RDONLY | os.O_NONBLOCK)


def send(kind, code, value):
    os.write(gamepad, event.pack(0, 0, kind, code, value))
    os.write(gamepad, event.pack(0, 0, 0, 0, 0))
    time.sleep(0.1)


send(1, 307, 1)
send(1, 307, 0)
send(3, 16, -1)
send(3, 16, 0)
send(3, 0, 32767)
send(3, 0, 0)
# Unplug with RB held. Its Tab key must be released by the bridge.
send(1, 311, 1)
fcntl.ioctl(gamepad, 0x5502)
os.close(gamepad)
time.sleep(1)
seen = []
while select.select([reader], [], [], 0.2)[0]:
    seen.extend((kind, code, value) for _, _, kind, code, value
                in event.iter_unpack(os.read(reader, event.size * 128)))
os.close(reader)
for expected in ((1, 66, 1), (1, 66, 0), (1, 105, 1), (1, 105, 0), (1, 15, 1), (1, 15, 0)):
    assert expected in seen, (expected, seen)
assert any(kind == 2 and code == 0 and value > 0 for kind, code, value in seen), seen
print("PASS: virtual gamepad, keyboard toggle, D-pad, pointer and hot-unplug release", flush=True)
