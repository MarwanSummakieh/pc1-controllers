"""Application input must be neutral whenever the appliance owns the pad."""
import importlib.util
import ctypes
import errno
import json
import struct
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("controller_router", Path(__file__).resolve().parents[1] /
    "os/files/usr/lib/marwanos/controller/router.py")
router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router)


class ControllerGateTests(unittest.TestCase):
    def test_linux_face_buttons_match_controller_positions(self):
        self.assertEqual(router.BUTTONS[304], 0)  # BTN_SOUTH: Cross / A
        self.assertEqual(router.BUTTONS[305], 1)  # BTN_EAST: Circle / B
        self.assertEqual(router.BUTTONS[308], 2)  # BTN_WEST: Square / X
        self.assertEqual(router.BUTTONS[307], 3)  # BTN_NORTH: Triangle / Y

    def test_shell_mode_neutralizes_every_application_control(self):
        gate = router.Gate()
        self.assertEqual(gate.output([1] * 15, [1.] * 6), ([0] * 15, [0.] * 6))

    def test_resume_does_not_forward_held_menu_press_or_stick(self):
        gate = router.Gate()
        buttons, axes = [0] * 15, [0.] * 6
        buttons[0], axes[0] = 1, .9
        gate.set_active(True, buttons, axes)
        self.assertEqual(gate.output(buttons, axes), ([0] * 15, [0.] * 6))
        gate.output([0] * 15, [0.] * 6)
        self.assertEqual(gate.output(buttons, axes), (buttons, axes))

    def test_guide_is_reserved_but_share_reaches_game(self):
        gate = router.Gate()
        gate.set_active(True, [0] * 15, [0.] * 6)
        buttons = [1] * 15
        output, _ = gate.output(buttons, [0.] * 6)
        self.assertEqual(output[4:6], [1, 0])
        self.assertEqual(output[0], 1)

    def test_share_event_keeps_game_input_active_and_guide_neutralizes_it(self):
        broker = router.Router.__new__(router.Router)
        slot = router.Slot(0)
        slot.device = {"slot": slot}
        slot.pad = Mock()
        broker.slots = [slot]
        slot.active(True)
        broker.event(slot.device, 1, 314, 1)
        self.assertTrue(slot.gate.active)
        self.assertEqual(slot.gate.output(slot.buttons, slot.axes)[0][4], 1)
        broker.event(slot.device, 1, 316, 1)
        self.assertFalse(slot.gate.active)

    def test_overlay_and_reconnect_do_not_leave_stuck_keys(self):
        gate = router.Gate()
        gate.set_active(True, [0] * 15, [0.] * 6)
        gate.output([1] * 15, [.8] * 6)
        gate.set_active(False, [1] * 15, [.8] * 6)
        self.assertEqual(gate.output([1] * 15, [.8] * 6), ([0] * 15, [0.] * 6))


class ControllerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.broker = router.Router.__new__(router.Router)
        self.slot = SimpleNamespace(buttons=[0] * 15, axes=[0.] * 6,
                                    gate=router.Gate(), pad=Mock(), rumble=Mock())
        self.device = {"fd": 10, "path": "/dev/input/event6",
                       "ranges": {0: (-32768, 32767), 1: (-32768, 32767)}, "slot": self.slot}
        self.slot.device = self.device
        self.broker.devices = {10: self.device}
        self.broker.slots = [self.slot]
        self.broker.publish = Mock()
        self.broker.disconnect = Mock()

    def test_nonblocking_read_does_not_disconnect_controller(self):
        with patch.object(router.os, 'read', side_effect=BlockingIOError()):
            self.broker.read_device(10)
        self.broker.disconnect.assert_not_called()
        self.assertEqual(self.device['fd'], 10)

    def test_dropped_frame_retains_grab_and_queries_current_state(self):
        self.slot.gate.set_active(True, self.slot.buttons, self.slot.axes)
        self.broker.event(self.device, 0, 3, 0)
        self.broker.event(self.device, 1, 305, 1)  # Invalid remainder of the lost frame.
        self.assertEqual(self.slot.buttons, [0] * 15)
        def ioctl(fd, request, values):
            self.assertEqual(fd, 10)
            if request == 0x80604518:
                values[304 // 8] |= 1 << (304 % 8)
            elif request == 0x80184540:
                values[0] = 32767
            elif request in (0x80184550, 0x80184551):
                raise OSError('No hat axes')
        with patch.object(router.fcntl, 'ioctl', side_effect=ioctl):
            self.broker.event(self.device, 0, 0, 0)
        self.broker.disconnect.assert_not_called()
        self.assertFalse(self.device['sync_lost'])
        self.assertEqual(self.slot.buttons[0:2], [1, 0])
        self.assertEqual(self.slot.axes[0], 1.)
        buttons, axes = self.slot.gate.output(self.slot.buttons, self.slot.axes)
        self.assertEqual(buttons, [0] * 15)
        self.assertEqual(axes[0], 0.)
        self.assertTrue(all(abs(axis) < .0001 for axis in axes))

    def test_real_read_failure_still_disconnects(self):
        with patch.object(router.os, 'read', side_effect=OSError('Device removed')):
            self.broker.read_device(10)
        self.broker.disconnect.assert_called_once_with(10)

    def test_discovery_skips_virtual_pads_before_any_evdev_open_or_ioctl(self):
        for name in (router.VIRTUAL_NAME, router.VIRTUAL_NAME + ' 2'):
            with self.subTest(name=name), patch.object(router.glob, 'glob', return_value=['/dev/input/event22']), \
                    patch.object(router.Path, 'read_text', return_value=name), \
                    patch.object(router.os, 'open') as open_device, patch.object(router.fcntl, 'ioctl') as ioctl:
                self.broker.scan()
                open_device.assert_not_called()
                ioctl.assert_not_called()

    def test_disappearing_sysfs_node_does_not_fall_back_to_blocking_evdev_probe(self):
        with patch.object(router.glob, 'glob', return_value=['/dev/input/event22']), \
                patch.object(router.Path, 'read_text', side_effect=FileNotFoundError()), \
                patch.object(router.os, 'open') as open_device:
            self.broker.scan()
        open_device.assert_not_called()

    def test_model_named_virtual_pad_is_skipped_using_physical_slot_label(self):
        with patch.object(router.glob, 'glob', return_value=['/dev/input/event22']), \
                patch.object(router.Path, 'read_text', side_effect=['Sony DualSense', 'pc1/application/slot1']), \
                patch.object(router.os, 'open') as open_device:
            self.broker.scan()
        open_device.assert_not_called()

    def test_steam_input_output_is_not_grabbed_as_a_physical_controller(self):
        with patch.object(router.glob, 'glob', return_value=['/dev/input/event22']), \
                patch.object(router.Path, 'read_text', side_effect=['Microsoft X-Box 360 pad', '']), \
                patch.object(router.Path, 'resolve', return_value=Path('/sys/devices/virtual/input/input42')), \
                patch.object(router.os, 'open') as open_device:
            self.broker.scan()
        open_device.assert_not_called()

    def test_bluetooth_uhid_is_still_a_physical_source(self):
        with patch.object(router.glob, 'glob', return_value=['/dev/input/event22']), \
                patch.object(router.Path, 'read_text', side_effect=['Sony DualSense', '', '1000000000000 0 0 0 0', '3']), \
                patch.object(router.Path, 'resolve', return_value=Path('/sys/devices/virtual/misc/uhid/0005:054C:0CE6/input/input42')), \
                patch.object(router.os, 'open', side_effect=PermissionError()) as open_device:
            self.broker.scan()
        open_device.assert_called()

    def test_discovery_never_opens_non_gamepad_nodes_across_repeated_scans(self):
        for keys, axes in [('0', '0'), ('2420 10000 0 0 0 0', '3'),
                           ('1000000000000 0 0 0 0', '1')]:
            def read(path, *args, **kwargs):
                return {'name': 'Physical input node', 'phys': 'usb/input0',
                        'key': keys, 'abs': axes}[path.name]
            with self.subTest(keys=keys, axes=axes), \
                    patch.object(router.glob, 'glob', return_value=['/dev/input/event22']), \
                    patch.object(router.Path, 'read_text', autospec=True, side_effect=read), \
                    patch.object(router.Path, 'resolve', return_value=Path('/sys/devices/pci/input42')), \
                    patch.object(router.os, 'open') as open_device, \
                    patch.object(router.os, 'close') as close_device:
                self.broker.scan()
                self.broker.scan()
            open_device.assert_not_called()
            close_device.assert_not_called()

    def test_disappearing_or_malformed_capabilities_do_not_probe_evdev(self):
        for error in (FileNotFoundError(), 'not a bitmap'):
            def read(path, *args, **kwargs):
                if path.name == 'name': return 'Physical controller'
                if path.name == 'phys': return 'usb/input0'
                if isinstance(error, Exception): raise error
                return error
            with self.subTest(error=error), \
                    patch.object(router.glob, 'glob', return_value=['/dev/input/event22']), \
                    patch.object(router.Path, 'read_text', autospec=True, side_effect=read), \
                    patch.object(router.Path, 'resolve', return_value=Path('/sys/devices/pci/input42')), \
                    patch.object(router.os, 'open') as open_device:
                self.broker.scan()
            open_device.assert_not_called()

    def test_sysfs_capability_words_preserve_zero_padding(self):
        self.assertEqual(router.capability_bits('1 2 3'), (1 << 128) | (2 << 64) | 3)
        self.assertTrue(router.capability_bits('7fdb000000000000 0 0 0 0') & (1 << 304))
        self.assertFalse(router.capability_bits('2420 10000 0 0 0 0') & (1 << 304))


class RumbleTests(unittest.TestCase):
    def setUp(self):
        self.rumble = router.Rumble()
        self.rumble.bind({"fd": 10, "rumble": True})
        self.effect = router.FFEffect()
        self.effect.type, self.effect.id = router.FF_RUMBLE, 3
        self.effect.replay_length = 500
        self.effect.u.rumble.strong_magnitude = 25000
        self.effect.u.rumble.weak_magnitude = 9000

    def upload(self):
        def ioctl(fd, request, effect):
            self.assertEqual((fd, request), (10, router.EVIOCSFF))
            self.assertEqual(effect.id, -1)
            self.assertEqual(effect.u.rumble.strong_magnitude, 25000)
            effect.id = 7
        with patch.object(router, "struct_ioctl", side_effect=ioctl):
            self.rumble.upload(self.effect)

    def test_upload_uses_physical_id_and_correct_magnitudes(self):
        self.upload()
        self.assertEqual(self.rumble.ids, {3: 7})

    def test_play_and_stop_translate_only_to_own_controller(self):
        self.upload()
        self.rumble.set_active(True)
        with patch.object(router.os, "write") as write:
            self.rumble.play(3, 2)
            self.rumble.play(3, 0)
        self.assertEqual([router.EVENT.unpack(call.args[1])[2:] for call in write.call_args_list],
                         [(router.EV_FF, 7, 2), (router.EV_FF, 7, 0)])
        self.assertTrue(all(call.args[0] == 10 for call in write.call_args_list))

    def test_menu_transition_cancels_motor_and_ignores_new_playback(self):
        self.upload()
        self.rumble.set_active(True)
        with patch.object(router.os, "write") as write:
            self.rumble.set_active(False)
            self.rumble.play(3, 10)
        write.assert_called_once_with(10, router.EVENT.pack(0, 0, router.EV_FF, 7, 0))

    def test_erase_clears_mapping_and_effect(self):
        self.upload()
        with patch.object(router.fcntl, "ioctl") as ioctl:
            self.rumble.erase(3)
        ioctl.assert_called_once_with(10, router.EVIOCRMFF, 7)
        self.assertEqual(self.rumble.ids, {})
        self.assertEqual(self.rumble.effects, {})

    def test_reconnect_never_auto_plays_and_reuploads_on_fresh_request(self):
        self.upload()
        with patch.object(router.os, "write") as write:
            self.rumble.bind(None)
            self.rumble.bind({"fd": 20, "rumble": True})
            self.rumble.set_active(True)
        write.assert_called_once_with(10, router.EVENT.pack(0, 0, router.EV_FF, 7, 0))
        def ioctl(fd, request, effect):
            self.assertEqual(fd, 20)
            self.assertEqual(effect.id, -1)
            effect.id = 2
        with patch.object(router, "struct_ioctl", side_effect=ioctl), patch.object(router.os, "write") as write:
            self.rumble.play(3, 1)
        write.assert_called_once_with(20, router.EVENT.pack(0, 0, router.EV_FF, 2, 1))

    def test_unsupported_and_disconnected_uploads_return_linux_errors(self):
        self.rumble.device["rumble"] = False
        with self.assertRaises(OSError) as failure:
            self.rumble.upload(self.effect)
        self.assertEqual(failure.exception.errno, errno.EOPNOTSUPP)
        self.rumble.bind(None)
        with self.assertRaises(OSError) as failure:
            self.rumble.upload(self.effect)
        self.assertEqual(failure.exception.errno, errno.ENODEV)

    def test_upload_callback_always_finishes_on_physical_failure(self):
        calls = []
        def ioctl(fd, request, operation):
            calls.append((fd, request, getattr(operation, "retval", 0)))
            if request == router.UI_BEGIN_FF_UPLOAD:
                operation.effect.type, operation.effect.id = router.FF_RUMBLE, 3
            elif request == router.EVIOCSFF:
                raise OSError(errno.ENOSPC, "physical effect slots full")
        with patch.object(router, "struct_ioctl", side_effect=ioctl):
            self.rumble.handle(40, router.EV_UINPUT, 1, 123)
        self.assertEqual(calls[-1], (40, router.UI_END_FF_UPLOAD, -errno.ENOSPC))

    def test_effect_update_retains_existing_native_id(self):
        self.upload()
        def ioctl(fd, request, effect):
            self.assertEqual(effect.id, 7)
        with patch.object(router, "struct_ioctl", side_effect=ioctl):
            self.rumble.upload(self.effect)

    def test_retired_upload_or_erase_request_does_not_remove_gamepads(self):
        for code, begin in ((1, router.UI_BEGIN_FF_UPLOAD), (2, router.UI_BEGIN_FF_ERASE)):
            with self.subTest(code=code), patch.object(router, "struct_ioctl",
                    side_effect=OSError(errno.EINVAL, "request already retired")) as ioctl, \
                    patch.object(self.rumble, "upload") as upload, patch.object(self.rumble, "erase") as erase:
                self.rumble.handle(40, router.EV_UINPUT, code, 123)
                ioctl.assert_called_once()
                self.assertEqual(ioctl.call_args.args[:2], (40, begin))
                upload.assert_not_called()
                erase.assert_not_called()

    def test_request_retiring_during_physical_callback_keeps_broker_alive(self):
        for code, end in ((1, router.UI_END_FF_UPLOAD), (2, router.UI_END_FF_ERASE)):
            calls = []
            def ioctl(fd, request, operation):
                calls.append(request)
                if request == end:
                    raise OSError(errno.EINVAL, "request timed out during physical ioctl")
            with self.subTest(code=code), patch.object(router, "struct_ioctl", side_effect=ioctl), \
                    patch.object(self.rumble, "upload"), patch.object(self.rumble, "erase"):
                self.rumble.handle(40, router.EV_UINPUT, code, 123)
                self.assertEqual(calls[-1], end)

    def test_unexpected_callback_ioctl_errors_are_not_hidden(self):
        for failing_request in (router.UI_BEGIN_FF_UPLOAD, router.UI_END_FF_UPLOAD):
            def ioctl(fd, request, operation):
                if request == failing_request:
                    raise OSError(errno.EIO, "unexpected device fault")
            with self.subTest(request=failing_request), patch.object(router, "struct_ioctl", side_effect=ioctl), \
                    patch.object(self.rumble, "upload"), self.assertRaises(OSError) as failure:
                self.rumble.handle(40, router.EV_UINPUT, 1, 123)
            self.assertEqual(failure.exception.errno, errno.EIO)


class VirtualDeviceLifecycleTests(unittest.TestCase):
    def test_virtual_identity_preserves_model_and_uses_distinct_virtual_transport(self):
        for identity in ((3, 0x054c, 0x0ce6, 0x8111), (5, 0x045e, 0x0b13, 0x0509), (3, 0x1234, 0xabcd, 1)):
            with self.subTest(identity=identity), patch.object(router.os, 'open', return_value=40), \
                    patch.object(router.fcntl, 'ioctl') as ioctl:
                pad = router.VirtualPad('Real model', input_id=identity)
                setup = next(call.args[2] for call in ioctl.call_args_list if call.args[1] == 0x405c5503)
                values = struct.unpack('HHHH80sI', setup)
                self.assertEqual(values[:4], (6, *identity[1:]))
                self.assertEqual(values[4].split(b'\0')[0], b'Real model')
                self.assertEqual((pad.keys[2], pad.keys[3]), (308, 307) if identity[1] == 0x054c else (307, 308))

    def test_physical_face_buttons_follow_vendor_semantics(self):
        for vendor, code in ((0x054c, 308), (0x045e, 307)):
            broker = router.Router.__new__(router.Router)
            slot = router.Slot(0)
            buttons = dict(router.BUTTONS)
            if vendor != 0x054c:
                buttons[307], buttons[308] = 2, 3
            broker.event({'slot': slot, 'buttons': buttons}, 1, code, 1)
            self.assertEqual(slot.buttons[2:4], [1, 0])

    def test_empty_slots_do_not_create_connected_application_devices(self):
        with patch.object(router, "VirtualPad") as create:
            slot = router.Slot(0, "remembered")
            slot.active(True)
        create.assert_not_called()
        self.assertIsNone(slot.pad)
        self.assertFalse(slot.gate.active)

    def test_disconnect_removes_only_matching_pad_and_clears_old_effects(self):
        broker = router.Router.__new__(router.Router)
        first, second = router.Slot(0, "first"), router.Slot(1, "second")
        first.pad, second.pad = Mock(), Mock()
        removed, remaining = first.pad, second.pad
        first.device = {"fd": 10, "slot": first}
        second.device = {"fd": 11, "slot": second}
        first.rumble.effects[3] = b"old device effect"
        broker.slots = [first, second]
        broker.devices = {10: first.device, 11: second.device}
        with patch.object(router.os, "close") as close:
            broker.disconnect(10)
            broker.disconnect(10)
        removed.close.assert_called_once()
        remaining.close.assert_not_called()
        close.assert_called_once_with(10)
        self.assertIsNone(first.pad)
        self.assertIsNone(first.device)
        self.assertEqual(first.identity, "first")
        self.assertEqual(first.rumble.effects, {})
        self.assertIs(second.pad, remaining)
        self.assertIn(11, broker.devices)

    def test_failed_virtual_setup_does_not_leak_uinput_fd(self):
        with patch.object(router.os, "open", return_value=40), \
                patch.object(router.VirtualPad, "configure", side_effect=OSError(errno.EIO, "setup failed")), \
                patch.object(router.os, "close") as close, self.assertRaises(OSError):
            router.VirtualPad()
        close.assert_called_once_with(40)

    def test_ready_feedback_is_not_read_after_physical_disconnect_removes_pad(self):
        broker = router.Router.__new__(router.Router)
        slot = router.Slot(0, "first")
        slot.pad = Mock(fd=20)
        removed = slot.pad
        slot.device = {"fd": 10, "slot": slot}
        broker.slots, broker.devices = [slot], {10: slot.device}
        broker.socket, broker.endpoint = Mock(), Mock()
        broker.stopping, broker.lease = False, float("inf")
        broker.scan, broker.publish = Mock(), Mock()
        def read(_fd):
            broker.disconnect(10)
            broker.stopping = True
        broker.read_device = read
        with patch.object(router.select, "select", return_value=([10, 20], [], [])), \
                patch.object(router.os, "close"):
            broker.run()
        removed.close.assert_called_once()
        removed.read_feedback.assert_not_called()


class MultiplayerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.broker = router.Router.__new__(router.Router)
        self.broker.slots_path = Path(self.temporary.name) / "slots.json"
        self.broker.slots = []
        for index in range(4):
            slot = SimpleNamespace(index=index, identity="", device=None, active=Mock(),
                                   buttons=[0] * 15, axes=[0.] * 6, rumble=Mock())
            self.broker.slots.append(slot)

    def test_serial_identity_reattaches_to_same_slot_without_displacing_other_pad(self):
        first, second = self.broker.claim("serial:first"), self.broker.claim("serial:second")
        first.device, second.device = {}, {"fd": 11}
        self.assertEqual((first.index, second.index), (0, 1))
        first.device = None
        self.assertIs(self.broker.claim("serial:first"), first)
        self.assertEqual(second.identity, "serial:second")
        self.assertEqual(json.loads(self.broker.slots_path.read_text())[:2], ["serial:first", "serial:second"])

    def test_new_pad_uses_unused_slot_before_disconnected_remembered_slot(self):
        first = self.broker.claim("first")
        self.assertEqual(self.broker.claim("replacement").index, 1)
        self.assertEqual(first.identity, "first")

    def test_home_on_player_two_neutralizes_every_application_slot(self):
        self.broker.slots[1].buttons[5] = 1
        self.broker.set_active(True)
        for slot in self.broker.slots:
            slot.active.assert_called_once_with(False)

    def test_share_on_any_player_does_not_disable_application_input(self):
        for player in range(4):
            with self.subTest(player=player):
                self.broker.slots[player].buttons[4] = 1
                self.broker.set_active(True)
                for slot in self.broker.slots:
                    slot.active.assert_called_once_with(True)
                    slot.active.reset_mock()
                self.broker.slots[player].buttons[4] = 0

    def test_full_connected_slots_do_not_displace_existing_players(self):
        for slot in self.broker.slots:
            slot.device = {"fd": slot.index}
        self.assertIsNone(self.broker.claim("fifth"))

    def test_replacement_clears_previous_controllers_effects(self):
        for slot in self.broker.slots:
            slot.identity = f"old{slot.index}"
        replaced = self.broker.claim("new")
        replaced.rumble.effects.clear.assert_called_once_with()

    def test_home_snapshot_keeps_player_two_buttons_out_of_shell_navigation(self):
        self.broker.peer, self.broker.lease = ("127.0.0.1", 999), float("inf")
        self.broker.sequence, self.broker.token, self.broker.socket = 0, "token", Mock()
        self.broker.slots[1].buttons[0] = 1
        self.broker.slots[1].buttons[5] = 1
        self.broker.publish()
        state = json.loads(self.broker.socket.sendto.call_args.args[0])
        self.assertEqual(state["buttons"][0], 0)
        self.assertEqual(state["buttons"][5], 1)
        self.assertTrue(state["home"])

    def test_share_snapshot_is_player_one_only_and_does_not_signal_home(self):
        self.broker.peer, self.broker.lease = ("127.0.0.1", 999), float("inf")
        self.broker.sequence, self.broker.token, self.broker.socket = 0, "token", Mock()
        for player in range(4):
            with self.subTest(player=player):
                self.broker.slots[player].buttons[4] = 1
                self.broker.publish()
                state = json.loads(self.broker.socket.sendto.call_args.args[0])
                self.assertEqual(state["buttons"][4], int(player == 0))
                self.assertFalse(state["home"])
                self.broker.slots[player].buttons[4] = 0

    def test_serial_identity_survives_usb_to_bluetooth_transport_change(self):
        identities = []
        for bus, version in ((3, 0x8111), (5, 0x100)):
            def ioctl(fd, request, value):
                if request == 0x80084502:
                    value[:] = struct.pack("HHHH", bus, 0x054c, 0x0ce6, version)
                elif request == 0x81004508:
                    value[:17] = b"14:3a:9a:e3:38:fe"
            with patch.object(router.fcntl, "ioctl", side_effect=ioctl):
                identities.append(self.broker.identity(10, "/dev/input/event6", "DualSense"))
        self.assertEqual(identities[0], identities[1])


if __name__ == "__main__":
    unittest.main()
