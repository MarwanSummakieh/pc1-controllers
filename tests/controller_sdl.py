#!/usr/bin/env python3
"""Root-only real uinput -> SDL model identification and control mapping check."""
import ctypes
import ctypes.util
import os
import time

from controller_kernel import device_path, router


def run():
    os.environ['SDL_JOYSTICK_HIDAPI'] = '0'
    os.environ['SDL_JOYSTICK_RAWINPUT'] = '0'
    os.environ['SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS'] = '1'
    os.environ.pop('SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT', None)
    os.environ.pop('SDL_GAMECONTROLLERCONFIG', None)
    sdl = ctypes.CDLL(ctypes.util.find_library('SDL2-2.0'))
    def bind(name, result, *args):
        function = getattr(sdl, 'SDL_' + name)
        function.restype, function.argtypes = result, args
        return function
    init = bind('Init', ctypes.c_int, ctypes.c_uint32)
    quit_sdl = bind('Quit', None)
    count = bind('NumJoysticks', ctypes.c_int)
    path_for_index = bind('JoystickPathForIndex', ctypes.c_char_p, ctypes.c_int)
    open_controller = bind('GameControllerOpen', ctypes.c_void_p, ctypes.c_int)
    close_controller = bind('GameControllerClose', None, ctypes.c_void_p)
    controller_type = bind('GameControllerGetType', ctypes.c_int, ctypes.c_void_p)
    controller_name = bind('GameControllerName', ctypes.c_char_p, ctypes.c_void_p)
    vendor = bind('GameControllerGetVendor', ctypes.c_uint16, ctypes.c_void_p)
    product = bind('GameControllerGetProduct', ctypes.c_uint16, ctypes.c_void_p)
    button = bind('GameControllerGetButton', ctypes.c_uint8, ctypes.c_void_p, ctypes.c_int)
    axis = bind('GameControllerGetAxis', ctypes.c_int16, ctypes.c_void_p, ctypes.c_int)
    update = bind('GameControllerUpdate', None)
    pump = bind('PumpEvents', None)
    fixtures = [
        ('Sony Interactive Entertainment DualSense Wireless Controller', (3, 0x054c, 0x0ce6, 0x8111), 7),
        ('Microsoft X-Box 360 pad', (3, 0x045e, 0x028e, 0x0114), 1),
        ('Unknown model', (3, 0x1234, 0xabcd, 1), 0),
    ]
    pads, handles = [], []
    try:
        for index, (name, identity, _kind) in enumerate(fixtures):
            pads.append(router.VirtualPad(name, rumble=False, physical=f'pc1/application/slot{index + 1}', input_id=identity))
            device_path(name, f'pc1/application/slot{index + 1}')
        assert init(0x2200) == 0, 'SDL joystick/controller initialization failed'
        for index, (name, identity, kind) in enumerate(fixtures):
            path = device_path(name, f'pc1/application/slot{index + 1}').encode()
            selected = next(i for i in range(count()) if path_for_index(i) == path)
            handle = open_controller(selected)
            assert handle, f'SDL failed to map {name}'
            handles.append(handle)
            assert (vendor(handle), product(handle)) == identity[1:3], 'SDL lost model IDs'
            assert controller_type(handle) == kind, (name, 'incorrect SDL type', controller_type(handle))
            def sample(buttons, axes):
                pads[index].update(buttons, axes)
                for _ in range(5):
                    pump()
                    update()
                    time.sleep(.01)
            for pressed in (0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 13, 14):
                sample([int(i == pressed) for i in range(15)], [0.] * 6)
                actual = [i for i in range(15) if button(handle, i)]
                assert actual == [pressed], (name, 'wrong mapped button', pressed, actual)
            sample([0] * 15, [.6, -.6, .4, -.4, .7, .8])
            actual_axes = [axis(handle, i) for i in range(6)]
            expected = [.6, -.6, .4, -.4, .7, .8]
            assert all(abs(actual / 32767 - value) < .03 for actual, value in zip(actual_axes, expected)), (name, actual_axes)
            print(f'SDL model/type/face buttons/dpad/sticks/triggers: {controller_name(handle).decode()}: PASS')
    finally:
        for handle in handles:
            close_controller(handle)
        quit_sdl()
        for pad in pads:
            pad.close()


if __name__ == '__main__':
    run()
