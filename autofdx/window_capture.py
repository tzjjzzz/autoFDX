"""HWND-based Windows Graphics Capture; never falls back to desktop pixels."""
import ctypes
from ctypes import wintypes as W
import inspect
import logging
import threading
from time import monotonic


class CaptureUnavailable(RuntimeError):
    pass


def frame_origin(width, height, bounds):
    """Resolve WGC frame rectangle without stretching existing calibrations."""
    for key in ('extended', 'window', 'client'):
        rect = bounds.get(key)
        if rect and (rect[2]-rect[0], rect[3]-rect[1]) == (width, height):
            return rect[0], rect[1]
    raise CaptureUnavailable(f"WGC frame {width}x{height} does not match window bounds; wait for resize/DPI update")


def frame_to_rgb(frame, bounds, requested):
    import numpy as np
    from PIL import Image
    h, width = frame.shape[:2]
    left, top = frame_origin(width, h, bounds)
    rx, ry, rw, rh = map(int, requested)
    wl, wt, wr, wb = bounds['window']
    if rw <= 0 or rh <= 0 or rx < wl or ry < wt or rx+rw > wr or ry+rh > wb:
        raise CaptureUnavailable('Requested capture region is outside the game window')
    # Preserve historical GetWindowRect calibration coordinates. WGC omits
    # invisible resize borders: only those absent border pixels are padded.
    out = np.zeros((rh, rw, 3), dtype=np.uint8)
    x1, y1 = max(rx, left), max(ry, top)
    x2, y2 = min(rx+rw, left+width), min(ry+rh, top+h)
    if x2 <= x1 or y2 <= y1:
        raise CaptureUnavailable('Capture crop does not intersect the WGC frame')
    out[y1-ry:y2-ry, x1-rx:x2-rx] = frame[y1-top:y2-top, x1-left:x2-left, :3][:, :, ::-1]
    return Image.fromarray(out)


class WindowGeometry:
    def __init__(self):
        self.u = ctypes.WinDLL('user32', use_last_error=True)
        self.dwm = ctypes.WinDLL('dwmapi')
        for name, result, arguments in [
            ('IsWindow', W.BOOL, [W.HWND]), ('IsIconic', W.BOOL, [W.HWND]),
            ('GetWindowThreadProcessId', W.DWORD, [W.HWND, ctypes.POINTER(W.DWORD)]),
            ('GetWindowRect', W.BOOL, [W.HWND, ctypes.POINTER(W.RECT)]),
            ('GetClientRect', W.BOOL, [W.HWND, ctypes.POINTER(W.RECT)]),
            ('ClientToScreen', W.BOOL, [W.HWND, ctypes.POINTER(W.POINT)]),
        ]:
            fn = getattr(self.u, name); fn.restype = result; fn.argtypes = arguments
        self.dwm.DwmGetWindowAttribute.argtypes = [W.HWND, W.DWORD, ctypes.c_void_p, W.DWORD]
        self.dwm.DwmGetWindowAttribute.restype = ctypes.c_long

    def identity(self, hwnd):
        if not hwnd or not self.u.IsWindow(hwnd):
            raise CaptureUnavailable('Game window is closed or missing')
        if self.u.IsIconic(hwnd):
            raise CaptureUnavailable('Game is minimized; WGC frame freshness is not guaranteed')
        pid = W.DWORD()
        if not self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(hwnd), pid.value

    def bounds(self, hwnd):
        def tup(r): return r.left, r.top, r.right, r.bottom
        rect = W.RECT()
        if not self.u.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())
        result = {'window': tup(rect)}
        ext = W.RECT()
        if self.dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(ext), ctypes.sizeof(ext)) == 0:
            result['extended'] = tup(ext)
        client = W.RECT(); origin = W.POINT(0, 0)
        if self.u.GetClientRect(hwnd, ctypes.byref(client)) and self.u.ClientToScreen(hwnd, ctypes.byref(origin)):
            result['client'] = (origin.x, origin.y, origin.x+client.right, origin.y+client.bottom)
        return result


def capture_factory(**kwargs):
    try:
        from windows_capture import WindowsCapture
    except ImportError as exc:
        raise CaptureUnavailable('Install WGC: python -m pip install -r requirements.txt') from exc
    if 'window_hwnd' not in inspect.signature(WindowsCapture).parameters:
        raise CaptureUnavailable('windows-capture lacks HWND support; upgrade to >=2.0.1')
    return WindowsCapture(**kwargs)


class WindowCapture:
    """One capture thread per game HWND/PID, shared by all recognition callers."""
    def __init__(self, hwnd_getter, *, geometry=None, factory=None, timeout=4.0, max_age=15.0):
        self._get_hwnd = hwnd_getter
        self._geometry = geometry or WindowGeometry()
        self._factory = factory or capture_factory
        self._timeout, self._max_age = float(timeout), float(max_age)
        self._lock = threading.RLock()
        self._condition = threading.Condition()
        self._identity = self._capture = self._control = None
        self._latest = None
        self._error = None
        self._generation = 0
        self._closed = False
        self._last_copy = 0.0
        self._stop_pending = None

    def _start(self, identity):
        self._close_locked()
        if self._stop_pending is not None and not self._stop_pending.is_set():
            raise CaptureUnavailable('Previous WGC capture is still stopping; retry later')
        with self._condition:
            self._identity = identity
            self._latest = None
            self._error = None
            self._last_copy = 0.0
            generation = self._generation
        capture = self._factory(window_hwnd=identity[0], monitor_index=None,
                                cursor_capture=False, draw_border=None)
        @capture.event
        def on_frame_arrived(frame, capture_control):
            try:
                now = monotonic()
                with self._condition:
                    if generation != self._generation:
                        return
                    # Immutable copies outlive the native callback's mapped buffer.
                    if self._latest is not None and now-self._last_copy < 0.03:
                        return
                    image = frame.frame_buffer[:, :, :3].copy()
                    self._latest = (now, int(frame.timespan), image)
                    self._last_copy = now
                    self._condition.notify_all()
            except Exception as exc:
                with self._condition:
                    if generation == self._generation:
                        self._error = str(exc); self._condition.notify_all()
                capture_control.stop()
        @capture.event
        def on_closed():
            with self._condition:
                if generation == self._generation:
                    self._error = 'WGC capture item closed'; self._condition.notify_all()
        self._capture = capture
        self._control = capture.start_free_threaded()

    def screenshot(self, region=None):
        with self._lock:
            if self._closed:
                raise CaptureUnavailable('Capture service has been shut down')
            try:
                identity = self._geometry.identity(self._get_hwnd())
                if identity != self._identity or self._control is None:
                    self._start(identity)
                deadline = monotonic()+self._timeout
                while True:
                    if self._control.is_finished():
                        raise CaptureUnavailable('WGC capture thread stopped')
                    bounds = self._geometry.bounds(identity[0])
                    with self._condition:
                        if self._error:
                            raise CaptureUnavailable(self._error)
                        latest = self._latest
                    if latest is not None:
                        received, timespan, image = latest
                        if monotonic()-received > self._max_age:
                            raise CaptureUnavailable(f'WGC has not delivered a frame for {self._max_age:g}s; reconnecting next request (static scenes may also stop callbacks)')
                        try:
                            frame_origin(image.shape[1], image.shape[0], bounds)
                        except CaptureUnavailable:
                            pass  # Wait for first correctly sized frame after resize.
                        else:
                            if region is None:
                                l,t,r,b = bounds['window']; region = (l,t,r-l,b-t)
                            return frame_to_rgb(image, bounds, region)
                    remaining = deadline-monotonic()
                    if remaining <= 0:
                        raise CaptureUnavailable('No correctly sized WGC frame within timeout')
                    with self._condition:
                        self._condition.wait(min(remaining, 0.1))
            except Exception as exc:
                self._close_locked()
                if isinstance(exc, CaptureUnavailable):
                    raise
                raise CaptureUnavailable(f"WGC capture failed: {exc}") from exc

    def diagnostics(self):
        with self._condition:
            latest = self._latest
            return {'identity': self._identity, 'error': self._error,
                    'age_seconds': None if latest is None else monotonic()-latest[0],
                    'timespan': None if latest is None else latest[1],
                    'frame_shape': None if latest is None else list(latest[2].shape)}

    def _close_locked(self):
        with self._condition:
            self._generation += 1
            capture = self._capture
            control, self._control = self._control, None
            self._capture = self._identity = self._latest = None
            self._error = None
            self._condition.notify_all()
        if control is None:
            return
        done = threading.Event()
        self._stop_pending = done
        def stop_capture():
            retained_capture = capture  # Keep native callback owner alive through stop/wait.
            try:
                control.stop()
                control.wait()
            except Exception:
                logging.exception('WGC capture cleanup failed')
            finally:
                done.set()
        threading.Thread(target=stop_capture, daemon=True, name='wgc-cleanup').start()
        if not done.wait(3):
            logging.error('WGC stop timed out; retaining stop barrier to prevent capture-thread leaks')

    def close(self):
        with self._lock:
            self._close_locked()

    def shutdown(self):
        with self._lock:
            self._closed = True
            self._close_locked()
