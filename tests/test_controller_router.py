"""Application input must be neutral whenever the appliance owns the pad."""
import importlib.util
from pathlib import Path
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
        self.broker.device = {"fd": 10, "ranges": {0: (-32768, 32767), 1: (-32768, 32767)}}
        self.broker.buttons, self.broker.axes = [0] * 15, [0.] * 6
        self.broker.gate = router.Gate()
        self.broker.pad = Mock()
        self.broker.publish = Mock()
        self.broker.disconnect = Mock()

    def test_nonblocking_read_does_not_disconnect_controller(self):
        with patch.object(router.os, 'read', side_effect=BlockingIOError()):
            self.broker.read_device(10)
        self.broker.disconnect.assert_not_called()
        self.assertEqual(self.broker.device['fd'], 10)

    def test_dropped_frame_retains_grab_and_queries_current_state(self):
        self.broker.gate.set_active(True, self.broker.buttons, self.broker.axes)
        self.broker.event(0, 3, 0)
        self.broker.event(1, 305, 1)  # Invalid remainder of the lost frame.
        self.assertEqual(self.broker.buttons, [0] * 15)
        def ioctl(fd, request, values):
            self.assertEqual(fd, 10)
            if request == 0x80604518:
                values[304 // 8] |= 1 << (304 % 8)
            elif request == 0x80184540:
                values[0] = 32767
            elif request in (0x80184550, 0x80184551):
                raise OSError('No hat axes')
        with patch.object(router.fcntl, 'ioctl', side_effect=ioctl):
            self.broker.event(0, 0, 0)
        self.broker.disconnect.assert_not_called()
        self.assertFalse(self.broker.device['sync_lost'])
        self.assertEqual(self.broker.buttons[0:2], [1, 0])
        self.assertEqual(self.broker.axes[0], 1.)
        buttons, axes = self.broker.gate.output(self.broker.buttons, self.broker.axes)
        self.assertEqual(buttons, [0] * 15)
        self.assertEqual(axes[0], 0.)
        self.assertTrue(all(abs(axis) < .0001 for axis in axes))

    def test_real_read_failure_still_disconnects(self):
        with patch.object(router.os, 'read', side_effect=OSError('Device removed')):
            self.broker.read_device(10)
        self.broker.disconnect.assert_called_once_with(10)


if __name__ == "__main__":
    unittest.main()
