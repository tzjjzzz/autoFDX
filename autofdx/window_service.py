import sys
import threading
from contextlib import contextmanager

import pyautogui

from .config import WINDOW_TITLE


class WindowService:
    """负责窗口定位和坐标转换。"""

    def __init__(self, config_store):
        self.config_store = config_store
        self._input = None
        self._input_lock = threading.RLock()
        self._input_shutdown = False
        self._capture = None
        self._capture_lock = threading.RLock()
        self._capture_shutdown = False

    @property
    def config(self):
        return self.config_store.data

    def get_game_window(self):
        title = self.config.get("window_title", WINDOW_TITLE)
        wins = pyautogui.getWindowsWithTitle(title)
        if not wins:
            raise RuntimeError(f"未找到窗口：{title}")
        return wins[0]

    def get_window_region(self):
        win = self.get_game_window()
        return win.left, win.top, win.width, win.height

    def denormalize_region(self, region):
        _, _, width, height = self.get_window_region()
        x1 = int(region[0] * width)
        y1 = int(region[1] * height)
        x2 = int(region[2] * width)
        y2 = int(region[3] * height)
        x1, x2 = sorted((max(0, x1), min(width, x2)))
        y1, y2 = sorted((max(0, y1), min(height, y2)))
        return x1, y1, x2, y2

    def denormalize_point(self, point):
        left, top, width, height = self.get_window_region()
        return int(left + point[0] * width), int(top + point[1] * height)

    def get_input(self):
        """Shared by automation and calibration; no duplicate Frida sessions."""
        if sys.platform != "win32":
            return None
        with self._input_lock:
            if self._input_shutdown:
                raise RuntimeError("Input service is shut down")
            if self._input is None:
                from .background_input import BackgroundInput
                self._input = BackgroundInput(lambda: self.get_game_window()._hWnd)
            return self._input

    def close_input(self):
        with self._input_lock:
            if self._input is not None:
                return self._input.close()
        return []

    def shutdown_input(self):
        with self._input_lock:
            self._input_shutdown = True
            if self._input is not None:
                return self._input.shutdown()
        return []

    @contextmanager
    def input_session(self):
        """Keep pause/cleanup from splitting calibration move + wheel replay."""
        with self._input_lock:
            try:
                yield self.get_input()
            finally:
                self.close_input()

    def screenshot(self, region=None):
        """PIL RGB image in existing screen/window coordinates, captured by HWND."""
        if sys.platform != "win32":
            return pyautogui.screenshot(region=region or self.get_window_region())
        with self._capture_lock:
            if self._capture_shutdown:
                raise RuntimeError("Capture service is shut down")
            if self._capture is None:
                from .window_capture import WindowCapture
                self._capture = WindowCapture(lambda: self.get_game_window()._hWnd)
            return self._capture.screenshot(region=region)

    def shutdown_capture(self):
        with self._capture_lock:
            self._capture_shutdown = True
            if self._capture is not None:
                self._capture.shutdown()
