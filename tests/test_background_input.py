"""No game, desktop input, Frida installation or graphics dependencies needed."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
import threading
import sys
import types
from unittest.mock import patch

from autofdx.background_input import BackgroundInput, InputUnavailable, packed_point, virtual_key


class FakeWin32:
    def __init__(self):
        self.pid = 123
        self.valid = True
        self.messages = []
        self.fg = 999
        self.failure = None
        self.origin = (100, 200)

    def identity(self, hwnd):
        if not self.valid:
            raise InputUnavailable('minimized or closed')
        return hwnd, self.pid

    def client_size(self, hwnd): return 800, 600
    def screen(self, hwnd, x, y): return x+self.origin[0], y+self.origin[1]
    def client(self, hwnd, x, y): return x-self.origin[0], y-self.origin[1]
    def foreground(self): return self.fg
    def foreground_thread(self): return 456
    def scan(self, vk): return 0xe04d if vk == 0x27 else 0x12

    def post(self, hwnd, message, wp=0, lp=0):
        self.messages.append((hwnd, message, wp, lp))
        if self.failure == message:
            raise OSError('injected post failure')


class FakeScript:
    def __init__(self):
        self.loaded = self.unloaded = self.stopped = False
        self.positions = []
        self.needs_activation = True
        self.raw = []
        self.cancelled = []
        self.exports_sync = self

    def on(self, event, callback): self.callback = callback
    def load(self): self.loaded = True
    def configure(self, hwnd): self.hwnd = hwnd
    def prepare(self, x, y):
        self.positions.append((x, y))
        result = self.needs_activation
        self.needs_activation = False
        return result
    def queue_raw(self, dx, dy):
        self.raw.append((dx, dy))
        return '0x123456'
    def cancel_raw(self, token): self.cancelled.append(token)
    def raw_status(self): return {'queued': len(self.raw), 'dataReads': 0, 'pending': len(self.raw)}
    def stop(self): self.stopped = True
    def unload(self): self.unloaded = True


class FakeSession:
    def __init__(self):
        self.script = FakeScript()
        self.detached = False
    def on(self, event, callback): self.callback = callback
    def create_script(self, agent):
        self.agent = agent
        return self.script
    def detach(self): self.detached = True


class BackgroundInputTests(unittest.TestCase):
    def setUp(self):
        self.sleeper = patch('autofdx.background_input.sleep')
        self.sleeper.start()
        self.addCleanup(self.sleeper.stop)
        self.api = FakeWin32()
        self.sessions = []
        def attach(pid):
            session = FakeSession()
            self.sessions.append(session)
            return session
        self.backend = BackgroundInput(lambda: 10, api=self.api, attach=attach)
        self.addCleanup(self.backend.shutdown)

    def test_move_translates_screen_to_client_without_physical_cursor(self):
        self.backend.move_to(130, 240)
        self.assertEqual(self.api.messages[-1], (10, 0x200, 0, packed_point(30, 40)))
        self.assertEqual(self.sessions[0].script.positions[-1], (130, 240))
        self.assertEqual(self.api.fg, 999)

    def test_activation_then_click_press_release(self):
        self.backend.move_to(130, 240)
        self.backend.left_click()
        self.assertEqual([m[1] for m in self.api.messages], [0x1c, 0x200, 0x1c, 0x200, 0x201, 0x202])
        self.assertEqual(self.api.messages[-2][2], 1)
        self.assertEqual(self.api.messages[-1][2], 0)

    def test_wheel_uses_screen_coords_and_preserves_signed_raw_delta(self):
        self.backend.move_to(130, 240)
        self.backend.scroll(-120)
        self.assertEqual(self.api.messages[-1], (10, 0x20a, 0xff880000, packed_point(130, 240)))
        self.backend.scroll(10)
        self.assertEqual(self.api.messages[-1][2], 10 << 16)

    def test_large_wheel_delta_is_split(self):
        self.backend.scroll(40000)
        messages = [m for m in self.api.messages if m[1] == 0x20a]
        self.assertEqual([m[2] >> 16 for m in messages], [32767, 7233])

    def test_keyboard_has_scan_and_up_bits(self):
        self.backend.press('e')
        down, up = self.api.messages[-2:]
        self.assertEqual(down[1:], (0x100, 0x45, 1 | (0x12 << 16)))
        self.assertEqual(up[1:], (0x101, 0x45, down[3] | (3 << 30)))
        self.assertFalse(self.backend._pressed)

    def test_extended_key(self):
        self.backend.press('right')
        self.assertTrue(self.api.messages[-2][3] & (1 << 24))

    def test_unknown_key_rejected_before_attach(self):
        with self.assertRaises(ValueError): self.backend.press('ctrl+e')
        self.assertEqual(self.sessions, [])

    def test_failed_mouse_down_still_attempts_release(self):
        self.api.failure = 0x201
        with self.assertRaises(OSError): self.backend.left_click()
        self.assertEqual(self.api.messages[-1][1], 0x202)

    def test_failed_key_down_still_attempts_release(self):
        self.api.failure = 0x100
        with self.assertRaises(OSError): self.backend.press('e')
        self.assertEqual(self.api.messages[-1][1], 0x101)

    def test_close_restores_actual_foreground_state_and_unloads(self):
        self.backend.left_click()
        session = self.sessions[0]
        self.backend.close()
        self.assertEqual(self.api.messages[-1][1:3], (0x1c, 0))
        self.assertTrue(session.script.stopped and session.script.unloaded and session.detached)
        count = len(self.api.messages)
        self.backend.close()
        self.assertEqual(len(self.api.messages), count)

    def test_close_does_not_fake_deactivation_when_game_is_foreground(self):
        self.backend.left_click()
        self.api.fg = 10
        self.backend.close()
        self.assertEqual(self.api.messages[-1][1:3], (0x1c, 1))

    def test_pause_close_reconnects_but_shutdown_rejects_late_workers(self):
        self.backend.press('e')
        self.backend.close()
        self.backend.press('e')
        self.assertEqual(len(self.sessions), 2)
        self.backend.shutdown()
        with self.assertRaises(InputUnavailable): self.backend.press('e')
        self.assertEqual(len(self.sessions), 2)

    def test_pid_change_rebinds(self):
        self.backend.press('e')
        self.api.pid = 124
        self.backend.press('e')
        self.assertEqual(len(self.sessions), 2)
        self.assertTrue(self.sessions[0].detached)

    def test_minimized_or_closed_releases_and_fails(self):
        self.backend.press('e')
        self.api.valid = False
        with self.assertRaises(InputUnavailable): self.backend.press('e')
        self.assertTrue(self.sessions[0].detached)

    def test_detached_session_fails_closed(self):
        self.backend.press('e')
        self.sessions[0].callback('process-terminated')
        with self.assertRaisesRegex(InputUnavailable, 'detached'): self.backend.press('e')

    def test_attachment_failure_has_no_global_input_fallback(self):
        self.backend._attach = lambda pid: (_ for _ in ()).throw(RuntimeError('denied'))
        with self.assertRaisesRegex(InputUnavailable, 'attachment failed'): self.backend.press('e')
        self.assertEqual(self.api.messages, [])

    def test_real_deactivation_rearms_app_state(self):
        self.backend.press('e')
        self.sessions[0].script.needs_activation = True
        self.backend.press('e')
        self.assertEqual(sum(m[1] == 0x1c and m[2] == 1 for m in self.api.messages), 2)

    def test_window_move_updates_screen_coords(self):
        self.backend.move_to(130, 240)
        self.api.origin = (500, 500)
        self.backend.left_click()
        self.assertEqual(self.sessions[0].script.positions[-1], (530, 540))

    def test_relative_motion_uses_virtual_origin_and_clamps(self):
        self.backend.move_to(130, 240)
        self.backend.move_rel(20, 10)
        self.assertEqual(self.backend._position, (50, 50))
        self.backend.move_rel(900, 900)
        self.assertEqual(self.backend._position, (799, 599))

    def test_outside_client_rejected(self):
        with self.assertRaisesRegex(InputUnavailable, 'outside'): self.backend.move_to(99, 200)

    def test_abort_prevents_keypress(self):
        self.assertFalse(self.backend.press_after_delay('e', abort_check=lambda: True))
        self.assertEqual(self.sessions, [])

    def test_wheel_calibration_reuses_target_backend(self):
        tree = ast.parse(Path('autofdx/actions.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'scroll_down_by_units')
        sent = []
        ns = {'sys': SimpleNamespace(platform='win32'), 'sleep': lambda _: None}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'scroll', 'exec'), ns)
        fn = ns['scroll_down_by_units']
        self.assertEqual(fn(240, input_backend=SimpleNamespace(scroll=sent.append)), 2)
        self.assertEqual(sent, [-120, -120])
        with self.assertRaises(RuntimeError): fn(120)

    def test_no_physical_input_api_in_windows_backend(self):
        source = Path('autofdx/background_input.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        banned = {'SendInput', 'SetForegroundWindow', 'SetCursorPos', 'keybd_event', 'mouse_event'}
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse(calls & banned)
        actions = Path('autofdx/actions.py').read_text(encoding='utf-8')
        self.assertNotIn('_send_wheel(', actions)
        self.assertNotIn('_send_relative_move(', actions)


    def test_concurrent_scroll_waits_until_click_release(self):
        entered = threading.Event()
        resume = threading.Event()
        errors = []
        def simulated_sleep(seconds):
            if abs(seconds - .08) < .0001:
                entered.set()
                if not resume.wait(2):
                    raise RuntimeError('test timeout')
        def call(fn):
            try: fn()
            except BaseException as exc: errors.append(exc)
        with patch('autofdx.background_input.sleep', side_effect=simulated_sleep):
            clicker = threading.Thread(target=lambda: call(self.backend.left_click))
            clicker.start()
            self.assertTrue(entered.wait(2))
            scroller = threading.Thread(target=lambda: call(lambda: self.backend.scroll(-120)))
            scroller.start()
            self.assertFalse(any(m[1] == 0x20a for m in self.api.messages))
            resume.set()
            clicker.join(2)
            scroller.join(2)
        self.assertFalse(clicker.is_alive() or scroller.is_alive())
        self.assertEqual(errors, [])
        messages = [m[1] for m in self.api.messages]
        self.assertLess(messages.index(0x202), messages.index(0x20a))

    def test_window_service_shares_backend_and_shutdown_is_permanent(self):
        # Import service without pyautogui or a real GUI installed.
        from importlib.util import spec_from_file_location, module_from_spec
        fake_gui = types.ModuleType('pyautogui')
        spec = spec_from_file_location('autofdx._test_window_service', 'autofdx/window_service.py')
        module = module_from_spec(spec)
        with patch.dict(sys.modules, {'pyautogui': fake_gui}), patch.object(sys, 'platform', 'win32'):
            spec.loader.exec_module(module)
            service = module.WindowService(SimpleNamespace(data={}))
            with patch('autofdx.background_input.BackgroundInput', return_value=self.backend) as factory:
                self.assertIs(service.get_input(), service.get_input())
                self.assertEqual(factory.call_count, 1)
                with service.input_session() as backend:
                    backend.press('e')
                self.assertTrue(self.sessions[0].detached)
                service.shutdown_input()
                with self.assertRaises(RuntimeError): service.get_input()

    def test_camera_uses_relative_raw_packet_without_moving_ui_cursor(self):
        self.backend.move_to(130, 240)
        self.api.messages.clear()
        self.backend.camera_move_rel(5000, -20)
        self.assertEqual(self.api.messages, [(10, 0xff, 1, 0x123456)])
        self.assertEqual(self.sessions[0].script.raw, [(5000, -20)])
        self.assertEqual(self.backend._position, (30, 40))
        self.assertEqual(self.backend.camera_status()['queued'], 1)

    def test_camera_failed_post_cancels_queued_token(self):
        self.api.failure = 0xff
        with self.assertRaises(OSError): self.backend.camera_move_rel(10, 0)
        self.assertEqual(self.sessions[0].script.cancelled, ['0x123456'])

    def test_camera_zero_delta_does_not_attach(self):
        self.backend.camera_move_rel(0, 0)
        self.assertFalse(self.sessions)

    def test_click_refreshes_activation_after_unobserved_manual_focus_cycle(self):
        self.backend.left_click()
        self.api.messages.clear()
        # Simulate a full user focus-in/focus-out between backend actions.
        # Even without a recorded transition or dirty flag, click re-arms Slate.
        self.sessions[0].script.needs_activation = False
        self.backend.left_click()
        messages=[m[1] for m in self.api.messages]
        self.assertEqual(messages, [0x1c, 0x200, 0x201, 0x202])

    def test_observed_foreground_exit_rearms_for_keyboard(self):
        self.api.fg = 10
        self.backend.press('e')
        self.api.messages.clear()
        self.api.fg = 999
        self.backend.press('e')
        self.assertEqual(self.api.messages[0][1:3], (0x1c, 1))

    def test_foreground_click_does_not_spam_activation(self):
        self.api.fg = 10
        self.backend.left_click()
        self.api.messages.clear()
        self.backend.left_click()
        self.assertEqual([m[1] for m in self.api.messages], [0x200, 0x201, 0x202])

    def test_signed_coordinates_and_named_keys(self):
        self.assertEqual(packed_point(-1, -2), 0xfffeffff)
        with self.assertRaises(InputUnavailable): packed_point(32768, 0)
        self.assertEqual(virtual_key('esc'), 27)
        self.assertEqual(virtual_key('f12'), 123)


if __name__ == '__main__':
    unittest.main()
