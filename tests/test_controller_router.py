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

    def test_home_controls_never_reach_game(self):
        gate = router.Gate()
        gate.set_active(True, [0] * 15, [0.] * 6)
        buttons = [1] * 15
        output, _ = gate.output(buttons, [0.] * 6)
        self.assertEqual(output[4:6], [0, 0])
        self.assertEqual(output[0], 1)

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
        self.device = {"fd": 10, "ranges": {0: (-32768, 32767), 1: (-32768, 32767)}, "slot": self.slot}
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
