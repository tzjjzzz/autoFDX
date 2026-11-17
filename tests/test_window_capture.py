import ast
from pathlib import Path
from types import SimpleNamespace
from time import monotonic
import unittest

import numpy as np

from autofdx.window_capture import CaptureUnavailable, WindowCapture, frame_origin, frame_to_rgb


class FakeGeometry:
    def __init__(self):
        self.pid = 100
        self.valid = True
        self.boxes = {'window':(10,20,16,26), 'extended':(11,21,15,25)}
    def identity(self, hwnd):
        if not self.valid:raise CaptureUnavailable('minimized')
        return hwnd, self.pid
    def bounds(self, hwnd):return self.boxes


class FakeControl:
    def __init__(self):self.finished=False;self.stopped=False;self.waited=False
    def is_finished(self):return self.finished
    def stop(self):self.stopped=True;self.finished=True
    def wait(self):self.waited=True


class FakeCapture:
    def __init__(self, frame, **kwargs):
        self.frame=frame;self.kwargs=kwargs;self.control=FakeControl()
    def event(self, fn):setattr(self,fn.__name__,fn);return fn
    def start_free_threaded(self):
        if self.frame is not None:self.emit(self.frame)
        return self.control
    def emit(self, image):
        self.on_frame_arrived(SimpleNamespace(frame_buffer=image, timespan=1234), self.control)


class WindowCaptureTests(unittest.TestCase):
    def setUp(self):
        self.geometry=FakeGeometry()
        self.frame=np.zeros((4,4,4),dtype=np.uint8)
        self.frame[:,:,:3]=[10,20,30]  # BGRA => RGB 30,20,10
        self.sessions=[]
        def factory(**kwargs):
            capture=FakeCapture(self.frame,**kwargs);self.sessions.append(capture);return capture
        self.capture=WindowCapture(lambda:12,geometry=self.geometry,factory=factory,timeout=.02)
        self.addCleanup(self.capture.shutdown)

    def test_window_target_not_monitor_and_no_cursor(self):
        image=self.capture.screenshot()
        self.assertEqual(self.sessions[0].kwargs,{'window_hwnd':12,'monitor_index':None,'cursor_capture':False,'draw_border':None})
        self.assertEqual(image.size,(6,6))

    def test_frame_mapping_preserves_legacy_window_border_offset(self):
        image=np.array(self.capture.screenshot())
        np.testing.assert_array_equal(image[1,1],[30,20,10])
        np.testing.assert_array_equal(image[0,0],[0,0,0])
        np.testing.assert_array_equal(image[4,4],[30,20,10])

    def test_crop_uses_absolute_coordinates_and_bgra_to_rgb(self):
        image=self.capture.screenshot((12,22,2,2))
        self.assertEqual(image.size,(2,2))
        np.testing.assert_array_equal(np.array(image)[0,0],[30,20,10])

    def test_client_only_frame_alignment(self):
        bounds={'window':(10,20,20,30),'client':(12,23,18,29)}
        self.assertEqual(frame_origin(6,6,bounds),(12,23))

    def test_negative_monitor_origin(self):
        bounds={'window':(-10,-10,-4,-4),'extended':(-9,-9,-5,-5)}
        image=frame_to_rgb(self.frame,bounds,(-8,-8,2,2))
        np.testing.assert_array_equal(np.array(image)[0,0],[30,20,10])

    def test_no_resizing_or_stretching_when_frame_bounds_mismatch(self):
        with self.assertRaises(CaptureUnavailable):frame_origin(3,3,self.geometry.boxes)
        self.geometry.boxes={'window':(0,0,7,7)}
        with self.assertRaisesRegex(CaptureUnavailable,'timeout'):self.capture.screenshot()
        self.assertTrue(self.sessions[0].control.stopped)

    def test_crop_outside_game_fails_instead_of_desktop_fallback(self):
        with self.assertRaisesRegex(CaptureUnavailable,'outside'):self.capture.screenshot((0,0,2,2))

    def test_callbacks_copy_native_buffer_before_return(self):
        self.capture.screenshot()
        self.frame[:]=255
        result=np.array(self.capture.screenshot((11,21,4,4)))
        np.testing.assert_array_equal(result[0,0],[30,20,10])

    def test_reuses_one_session_for_multiple_recognition_crops(self):
        self.capture.screenshot((11,21,1,1))
        self.capture.screenshot((12,22,1,1))
        self.assertEqual(len(self.sessions),1)

    def test_no_frames_times_out_even_without_callback(self):
        self.frame=None
        with self.assertRaisesRegex(CaptureUnavailable,'timeout'):self.capture.screenshot()
        self.assertTrue(self.sessions[0].control.stopped)

    def test_stale_frame_rejected_then_reconnects_on_next_request(self):
        self.capture.screenshot()
        with self.capture._condition:
            _,ts,data=self.capture._latest
            self.capture._latest=(monotonic()-20,ts,data)
        with self.assertRaisesRegex(CaptureUnavailable,'not delivered'):self.capture.screenshot()
        self.capture.screenshot()
        self.assertEqual(len(self.sessions),2)

    def test_pid_change_stops_previous_capture(self):
        self.capture.screenshot()
        self.geometry.pid=101
        self.capture.screenshot()
        self.assertEqual(len(self.sessions),2)
        self.assertTrue(self.sessions[0].control.stopped)

    def test_finished_capture_fails_then_reconnects(self):
        self.capture.screenshot()
        self.sessions[0].control.finished=True
        with self.assertRaisesRegex(CaptureUnavailable,'stopped'):self.capture.screenshot()
        self.capture.screenshot()
        self.assertEqual(len(self.sessions),2)

    def test_old_callbacks_cannot_poison_new_session(self):
        self.capture.screenshot()
        old=self.sessions[0]
        self.geometry.pid=102
        self.capture.screenshot()
        old.on_closed()
        self.assertIsNone(self.capture.diagnostics()['error'])
        self.capture.screenshot()

    def test_window_close_or_minimize_stops_capture(self):
        self.capture.screenshot()
        self.geometry.valid=False
        with self.assertRaises(CaptureUnavailable):self.capture.screenshot()
        self.assertTrue(self.sessions[0].control.stopped)

    def test_shutdown_rejects_late_workers(self):
        self.capture.screenshot()
        self.capture.shutdown()
        with self.assertRaisesRegex(CaptureUnavailable,'shut down'):self.capture.screenshot()
        self.assertTrue(self.sessions[0].control.waited)

    def test_capture_api_error_does_not_create_desktop_fallback(self):
        self.capture._factory=lambda **kwargs: (_ for _ in ()).throw(RuntimeError('not supported'))
        with self.assertRaisesRegex(CaptureUnavailable,'not supported'):self.capture.screenshot()
        self.assertIsNone(self.capture._control)


    def test_window_service_shares_capture_and_shutdown_is_permanent(self):
        import sys
        import types
        from unittest.mock import Mock, patch
        from importlib.util import spec_from_file_location, module_from_spec
        gui = types.ModuleType('pyautogui')
        gui.screenshot = Mock(side_effect=AssertionError('desktop capture used'))
        gui.getWindowsWithTitle = Mock(return_value=[SimpleNamespace(_hWnd=12)])
        spec = spec_from_file_location('autofdx._capture_test_service', 'autofdx/window_service.py')
        module = module_from_spec(spec)
        with patch.dict(sys.modules, {'pyautogui': gui}), patch.object(sys, 'platform', 'win32'):
            spec.loader.exec_module(module)
            service = module.WindowService(SimpleNamespace(data={}))
            with patch('autofdx.window_capture.WindowCapture', return_value=self.capture) as factory:
                service.screenshot((11,21,1,1))
                service.screenshot((12,22,1,1))
                self.assertEqual(factory.call_count, 1)
                self.assertEqual(factory.call_args.args[0](), 12)
                service.shutdown_capture()
                with self.assertRaises(RuntimeError):
                    service.screenshot()
                gui.screenshot.assert_not_called()

    def test_geometry_errors_are_reported_as_capture_errors(self):
        from unittest.mock import patch
        with patch.object(self.geometry, 'bounds', side_effect=OSError('window lost')):
            with self.assertRaisesRegex(CaptureUnavailable, 'window lost'):
                self.capture.screenshot()
        self.assertTrue(self.sessions[0].control.stopped)

    def test_app_screenshot_sites_all_use_window_service(self):
        count=0
        for path in ['autofdx/actions.py','autofdx/vision_service.py','autofdx/ui.py']:
            tree=ast.parse(Path(path).read_text(encoding='utf-8'))
            for n in ast.walk(tree):
                if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='screenshot':
                    self.assertEqual(ast.unparse(n.func.value),'self.window_service')
                    count+=1
        self.assertEqual(count,8)


if __name__=='__main__':unittest.main()
