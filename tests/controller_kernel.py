#!/usr/bin/env python3
"""Real Linux evdev/uinput multiplayer and force-feedback acceptance, as root."""
import errno
import importlib.util
import json
import os
from pathlib import Path
import select
import socket
import stat
import tempfile
import threading
import time
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("router", Path(__file__).resolve().parents[1] /
    "os/files/usr/lib/marwanos/controller/router.py")
router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router)


def wait_for(predicate, label, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError(label)


def device_path(name):
    result = []
    def find():
        for path in Path("/sys/class/input").glob("event*"):
            if (path / "device/name").read_text().strip() == name:
                target = Path("/dev/input") / path.name
                if not target.exists():
                    # WSL may expose sysfs uinput events without a udev daemon.
                    major, minor = map(int, (path / "dev").read_text().strip().split(":"))
                    target.parent.mkdir(exist_ok=True)
                    os.mknod(target, stat.S_IFCHR | 0o600, os.makedev(major, minor))
                result.append(str(target))
                return True
    wait_for(find, "uinput device did not appear: " + name)
    return result[0]


def events(fd, duration=.08):
    result = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], max(0, deadline - time.monotonic()))
        if readable:
            result.extend(router.EVENT.iter_unpack(os.read(fd, router.EVENT.size * 128)))
    return result


class Physical:
    """A kernel-backed physical fixture completing real FF ioctl callbacks."""
    def __init__(self, index, rumble=True):
        self.pad = router.VirtualPad(f"PC1 physical fixture {index}", rumble,
                                     physical=f"pc1-test-port-{index}")
        self.path = device_path(f"PC1 physical fixture {index}")
        self.buttons, self.axes = [0] * 15, [0.] * 6
        self.effects, self.playback, self.erased = {}, [], []
        self.stopping, self.fail_upload = False, False
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self):
        while not self.stopping:
            ready, _, _ = select.select([self.pad.fd], [], [], .02)
            if not ready:
                continue
            for _, _, kind, code, value in router.EVENT.iter_unpack(os.read(self.pad.fd, router.EVENT.size * 128)):
                if kind == router.EV_UINPUT and code == 1:
                    upload = router.FFUpload(value, 0)
                    router.struct_ioctl(self.pad.fd, router.UI_BEGIN_FF_UPLOAD, upload)
                    if self.fail_upload:
                        upload.retval = -errno.ENOSPC
                    else:
                        self.effects[upload.effect.id] = bytes(upload.effect)
                    router.struct_ioctl(self.pad.fd, router.UI_END_FF_UPLOAD, upload)
                elif kind == router.EV_UINPUT and code == 2:
                    erase = router.FFErase(value, 0, 0)
                    router.struct_ioctl(self.pad.fd, router.UI_BEGIN_FF_ERASE, erase)
                    self.effects.pop(erase.effect_id, None)
                    self.erased.append(erase.effect_id)
                    router.struct_ioctl(self.pad.fd, router.UI_END_FF_ERASE, erase)
                elif kind == router.EV_FF:
                    self.playback.append((code, value))

    def button(self, index, pressed):
        self.buttons[index] = int(pressed)
        self.pad.update(self.buttons, self.axes)

    def close(self):
        self.stopping = True
        self.thread.join(3)
        self.pad.close()


def run():
    with tempfile.TemporaryDirectory(prefix="pc1-controller-kernel-") as temporary:
        first, second, unsupported = Physical(1), Physical(2), Physical(3, False)
        fixtures = [first, second, unsupported]
        paths = [fixture.path for fixture in fixtures]
        observer = os.open(first.path, os.O_RDONLY | os.O_NONBLOCK)
        broker = router.Router(temporary)
        output = [os.open(device_path(router.VIRTUAL_NAME if slot.index == 0 else
                    f"{router.VIRTUAL_NAME} {slot.index + 1}"), os.O_RDWR | os.O_NONBLOCK)
                  for slot in broker.slots]
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.bind(("127.0.0.1", 0))
        client.settimeout(2)
        endpoint = json.loads(broker.endpoint.read_text())
        address = ("127.0.0.1", endpoint["port"])
        def mode(app):
            client.sendto(json.dumps({"token": endpoint["token"], "app": app}).encode(), address)
            time.sleep(.04)
        def upload(slot, strong=25000, weak=9000, effect_id=-1):
            effect = router.FFEffect()
            effect.type, effect.id = router.FF_RUMBLE, effect_id
            effect.replay_length = 1000
            effect.u.rumble.strong_magnitude = strong
            effect.u.rumble.weak_magnitude = weak
            router.struct_ioctl(output[slot], router.EVIOCSFF, effect)
            return effect.id
        def play(slot, effect, count=1):
            os.write(output[slot], router.EVENT.pack(0, 0, router.EV_FF, effect, count))
            time.sleep(.04)
        thread = threading.Thread(target=broker.run)
        try:
            with patch.object(router.glob, "glob", side_effect=lambda _pattern: list(paths)):
                thread.start()
                wait_for(lambda: len(broker.devices) == 3, "physical fixtures not grabbed")
                mode(False)
                first.button(0, True)
                second.button(1, True)
                time.sleep(.04)
                assert not [e for e in events(observer) if e[2] == 1], "physical events escaped exclusive grab"
                assert not [e for fd in output for e in events(fd) if e[2] == 1 and e[4]], "menu input reached application"
                state = None
                while state is None or not state["buttons"][0]:
                    state = json.loads(client.recv(8192))
                assert not state["buttons"][1], "player two moved shell navigation"
                mode(True)
                assert not [e for fd in output for e in events(fd) if e[2] == 1 and e[4]], "held confirm leaked on resume"
                first.button(0, False)
                second.button(1, False)
                time.sleep(.04)
                first.button(0, True)
                second.button(1, True)
                assert any(e[2:] == (1, 304, 1) for e in events(output[0])), "player one fresh A missing"
                assert any(e[2:] == (1, 305, 1) for e in events(output[1])), "player two fresh B missing"
                assert not [e for e in events(output[2]) if e[2] == 1 and e[4]], "player input crossed slots"
                mode(False)
                assert any(e[2:] == (1, 304, 0) for e in events(output[0])), "overlay did not release player one"
                assert any(e[2:] == (1, 305, 0) for e in events(output[1])), "overlay did not release player two"
                first.button(0, False)
                second.button(1, False)
                mode(True)

                effects = [upload(0), upload(1, 10000, 22000)]
                assert router.FFEffect.from_buffer_copy(next(iter(first.effects.values()))).u.rumble.strong_magnitude == 25000
                assert router.FFEffect.from_buffer_copy(next(iter(second.effects.values()))).u.rumble.weak_magnitude == 22000
                play(0, effects[0])
                assert first.playback[-1][1] == 1 and not second.playback, "rumble went to wrong controller"
                play(1, effects[1], 2)
                assert second.playback[-1][1] == 2, "second pad rumble missing"
                upload(0, 30000, 12000, effects[0])
                assert len(first.effects) == 1, "effect update leaked physical effects"
                assert router.FFEffect.from_buffer_copy(next(iter(first.effects.values()))).u.rumble.strong_magnitude == 30000
                mode(False)
                wait_for(lambda: first.playback[-1][1] == 0 and second.playback[-1][1] == 0, "menu did not cancel both motors")
                previous = len(first.playback)
                play(0, effects[0])
                assert len(first.playback) == previous, "menu allowed new rumble"
                mode(True)
                play(0, effects[0])
                second.button(5, True)
                wait_for(lambda: first.playback[-1][1] == 0, "player two Home did not cancel rumble")
                assert not broker.slots[0].gate.active and not broker.slots[1].gate.active
                assert not [e for fd in output for e in events(fd) if e[2] == 1 and e[3] in (314, 316) and e[4]], "Home escaped to game"
                second.button(5, False)
                mode(True)
                play(0, effects[0])
                wait_for(lambda: not broker.slots[0].gate.active, "lease did not expire", timeout=2)
                wait_for(lambda: first.playback[-1][1] == 0, "lease loss did not cancel motor")

                mode(True)
                router.fcntl.ioctl(output[1], router.EVIOCRMFF, effects[1])
                wait_for(lambda: bool(second.erased), "physical erase callback missing")
                assert not second.effects, "physical erased effect retained"
                try:
                    upload(2)
                    raise AssertionError("unsupported pad accepted rumble")
                except OSError as failure:
                    assert failure.errno == errno.EOPNOTSUPP
                first.fail_upload = True
                try:
                    upload(0)
                    raise AssertionError("physical upload failure not returned to game")
                except OSError as failure:
                    assert failure.errno == errno.ENOSPC
                first.fail_upload = False

                old_identity = broker.slots[0].identity
                paths.remove(first.path)
                first.close()
                fixtures.remove(first)
                wait_for(lambda: broker.slots[0].device is None, "disconnect did not clear player one")
                try:
                    upload(0)
                    raise AssertionError("disconnected pad accepted rumble")
                except OSError as failure:
                    assert failure.errno == errno.ENODEV
                first = Physical(1)
                fixtures.append(first)
                paths.append(first.path)
                wait_for(lambda: broker.slots[0].device is not None, "reconnect did not reattach player one")
                assert broker.slots[0].identity == old_identity
                assert broker.slots[1].device["path"] == second.path, "reconnect displaced player two"
                assert not first.playback, "reconnect restarted rumble automatically"
                mode(True)
                play(0, effects[0])
                assert first.playback[-1][1] == 1 and first.effects, "fresh request did not restore reconnected pad effect"
                print("Controller kernel checks: exclusive grabs, independent multiplayer slots, menu/lease/Home gating, held suppression, rumble upload/update/play/cancel/erase, errors and identity reconnect PASS")
        finally:
            broker.stopping = True
            thread.join(3)
            assert not thread.is_alive(), "controller broker did not stop"
            for fixture in fixtures:
                fixture.close()
            os.close(observer)
            for fd in output:
                os.close(fd)
            client.close()


if __name__ == "__main__":
    run()
